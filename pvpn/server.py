import argparse, asyncio, io, os, enum, struct, collections, hashlib, ipaddress, socket, random
import pproxy
from . import enums, message, crypto, ip, dns
from .__doc__ import *

class State(enum.Enum):
    INITIAL = 0
    SA_SENT = 1
    ESTABLISHED = 2
    DELETED = 3
    KE_SENT = 4
    HASH_SENT = 5
    AUTH_SET = 6
    CONF_SENT = 7
    CHILD_SA_SENT = 8

class ChildSa:
    def __init__(self, spi_in, spi_out, crypto_in, crypto_out):
        self.spi_in = spi_in
        self.spi_out = spi_out
        self.crypto_in = crypto_in
        self.crypto_out = crypto_out
        self.msgid_in = 1
        self.msgid_out = 1
        self.msgwin_in = set()
        self.child = None
    def incr_msgid_in(self):
        self.msgid_in += 1
        while self.msgid_in in self.msgwin_in:
            self.msgwin_in.discard(self.msgid_in)
            self.msgid_in += 1

class IKEv1Session:
    all_child_sa = {}
    def __init__(self, args, sessions, peer_spi, remote_id):
        self.args = args
        self.sessions = sessions
        self.my_spi = os.urandom(8)
        self.peer_spi = peer_spi
        self.crypto = None
        self.my_nonce = os.urandom(32)
        self.peer_nonce = None
        self.child_sa = self.all_child_sa.setdefault(remote_id, [])
        self.state = State.INITIAL
        self.sessions[self.my_spi] = self
    def response(self, exchange, payloads, message_id=0, *, crypto=None, hashmsg=None):
        if hashmsg:
            message_id = message_id or random.randrange(1<<32)
            buf = (b'' if hashmsg is True else hashmsg) + message.Message.encode_payloads(payloads)
            hash_r = self.crypto.prf.prf(self.skeyid_a, message_id.to_bytes(4, 'big') + buf)
            payloads.insert(0, message.PayloadHASH_1(hash_r))
        response = message.Message(self.peer_spi, self.my_spi, 0x10, exchange,
                enums.MsgFlag.NONE, message_id, payloads)
        print(repr(response))
        return response.to_bytes(crypto=crypto)
    def verify_hash(self, request):
        payload_hash = request.payloads.pop(0)
        assert payload_hash.type == enums.Payload.HASH_1
        hash_i = self.crypto.prf.prf(self.skeyid_a, request.message_id.to_bytes(4, 'big') + message.Message.encode_payloads(request.payloads))
        assert hash_i == payload_hash.data
    def xauth_init(self):
        attrs = { enums.CPAttrType.XAUTH_TYPE: 0,
                  enums.CPAttrType.XAUTH_USER_NAME: b'',
                  enums.CPAttrType.XAUTH_USER_PASSWORD: b'',
                }
        response_payloads = [message.PayloadCP_1(enums.CFGType.CFG_REQUEST, attrs)]
        return self.response(enums.Exchange.TRANSACTION_1, response_payloads, crypto=self.crypto, hashmsg=True)
    def process(self, request, stream, reply):
        request.parse_payloads(stream, crypto=self.crypto)
        print(repr(request))
        if request.exchange == enums.Exchange.IDENTITY_1 and request.get_payload(enums.Payload.SA_1):
            assert self.state == State.INITIAL
            request_payload_sa = request.get_payload(enums.Payload.SA_1)
            self.sa_bytes = request_payload_sa.to_bytes()
            self.transform = request_payload_sa.proposals[0].transforms[0].values
            del request_payload_sa.proposals[0].transforms[1:]
            response_payloads = request.payloads
            reply(self.response(enums.Exchange.IDENTITY_1, response_payloads))
            self.state = State.SA_SENT
        elif request.exchange == enums.Exchange.IDENTITY_1 and request.get_payload(enums.Payload.KE_1):
            assert self.state == State.SA_SENT
            self.peer_public_key = request.get_payload(enums.Payload.KE_1).ke_data
            self.my_public_key, self.shared_secret = crypto.DiffieHellman(self.transform[enums.TransformAttr.DH], self.peer_public_key)
            self.peer_nonce = request.get_payload(enums.Payload.NONCE_1).nonce
            response_payloads = [ message.PayloadKE_1(self.my_public_key), message.PayloadNONCE_1(self.my_nonce),
                                  message.PayloadNATD_1(os.urandom(32)), message.PayloadNATD_1(os.urandom(32)) ]
            cipher = crypto.Cipher(self.transform[enums.TransformAttr.ENCR], self.transform[enums.TransformAttr.KEY_LENGTH])
            prf = crypto.Prf(self.transform[enums.TransformAttr.HASH])
            self.skeyid = prf.prf(self.args.passwd.encode(), self.peer_nonce+self.my_nonce)
            self.skeyid_d = prf.prf(self.skeyid, self.shared_secret+self.peer_spi+self.my_spi+bytes([0]))
            self.skeyid_a = prf.prf(self.skeyid, self.skeyid_d+self.shared_secret+self.peer_spi+self.my_spi+bytes([1]))
            self.skeyid_e = prf.prf(self.skeyid, self.skeyid_a+self.shared_secret+self.peer_spi+self.my_spi+bytes([2]))
            iv = prf.hasher(self.peer_public_key+self.my_public_key).digest()[:cipher.block_size]
            self.crypto = crypto.Crypto(cipher, self.skeyid_e[:cipher.key_size], prf=prf, iv=iv)
            reply(self.response(enums.Exchange.IDENTITY_1, response_payloads))
            self.state = State.KE_SENT
        elif request.exchange == enums.Exchange.IDENTITY_1 and request.get_payload(enums.Payload.ID_1):
            assert self.state == State.KE_SENT
            payload_id = request.get_payload(enums.Payload.ID_1)
            prf = self.crypto.prf
            hash_i = prf.prf(self.skeyid, self.peer_public_key+self.my_public_key+self.peer_spi+self.my_spi+self.sa_bytes+payload_id.to_bytes())
            assert hash_i == request.get_payload(enums.Payload.HASH_1).data, 'Authentication Failed'
            response_payload_id = message.PayloadID_1(enums.IDType.ID_FQDN, f'{__title__}-{__version__}'.encode())
            hash_r = prf.prf(self.skeyid, self.my_public_key+self.peer_public_key+self.my_spi+self.peer_spi+self.sa_bytes+response_payload_id.to_bytes())
            response_payloads = [response_payload_id, message.PayloadHASH_1(hash_r)]
            reply(self.response(enums.Exchange.IDENTITY_1, response_payloads, crypto=self.crypto))
            self.state = State.HASH_SENT
            reply(self.xauth_init())
        elif request.exchange == enums.Exchange.TRANSACTION_1:
            self.verify_hash(request)
            payload_cp = request.get_payload(enums.Payload.CP_1)
            if enums.CPAttrType.XAUTH_USER_NAME in payload_cp.attrs:
                assert self.state == State.HASH_SENT
                response_payloads = [ message.PayloadCP_1(enums.CFGType.CFG_SET, {enums.CPAttrType.XAUTH_STATUS: 1}) ]
                self.state = State.AUTH_SET
            elif enums.CPAttrType.INTERNAL_IP4_ADDRESS in payload_cp.attrs:
                assert self.state == State.AUTH_SET
                attrs = { enums.CPAttrType.INTERNAL_IP4_ADDRESS: ipaddress.ip_address('10.0.0.1').packed,
                          enums.CPAttrType.INTERNAL_IP4_DNS: ipaddress.ip_address(self.args.dns).packed,
                        }
                response_payloads = [ message.PayloadCP_1(enums.CFGType.CFG_REPLY, attrs, identifier=payload_cp.identifier) ]
                self.state = State.CONF_SENT
            elif payload_cp.cftype == enums.CFGType.CFG_ACK:
                return
            else:
                raise Exception('Unknown CP Exchange')
            reply(self.response(enums.Exchange.TRANSACTION_1, response_payloads, request.message_id, crypto=self.crypto, hashmsg=True))
        elif request.exchange == enums.Exchange.QUICK_1 and len(request.payloads) == 1:
            assert request.payloads[0].type == enums.Payload.HASH_1
            assert self.state == State.CHILD_SA_SENT
            self.state = State.ESTABLISHED
        elif request.exchange == enums.Exchange.QUICK_1:
            assert self.state == State.CONF_SENT or self.child_sa
            self.verify_hash(request)
            payload_nonce = request.get_payload(enums.Payload.NONCE_1)
            peer_nonce = payload_nonce.nonce
            payload_nonce.nonce = my_nonce = os.urandom(len(peer_nonce))
            chosen_proposal = request.get_payload(enums.Payload.SA_1).proposals[0]
            del chosen_proposal.transforms[1:]
            peer_spi = chosen_proposal.spi
            chosen_proposal.spi = my_spi = os.urandom(4)
            reply(self.response(enums.Exchange.QUICK_1, request.payloads, request.message_id, crypto=self.crypto, hashmsg=peer_nonce))

            transform = chosen_proposal.transforms[0].values
            cipher = crypto.Cipher(chosen_proposal.transforms[0].id, transform[enums.ESPAttr.KEY_LENGTH])
            integ = crypto.Integrity(transform[enums.ESPAttr.AUTH])
            keymat = self.crypto.prf.prfplus_1(self.skeyid_d, bytes([chosen_proposal.protocol])+my_spi+peer_nonce+my_nonce, integ.key_size+cipher.key_size)
            sk_ei, sk_ai = struct.unpack('>{0}s{1}s'.format(cipher.key_size, integ.key_size), keymat)
            keymat = self.crypto.prf.prfplus_1(self.skeyid_d, bytes([chosen_proposal.protocol])+peer_spi+peer_nonce+my_nonce, integ.key_size+cipher.key_size)
            sk_er, sk_ar = struct.unpack('>{0}s{1}s'.format(cipher.key_size, integ.key_size), keymat)
            crypto_in = crypto.Crypto(cipher, sk_ei, integ, sk_ai)
            crypto_out = crypto.Crypto(cipher, sk_er, integ, sk_ar)
            child_sa = ChildSa(my_spi, peer_spi, crypto_in, crypto_out)
            self.sessions[my_spi] = child_sa
            for old_child_sa in self.child_sa:
                old_child_sa.child = child_sa
            self.child_sa.append(child_sa)
            self.state = State.CHILD_SA_SENT
        elif request.exchange == enums.Exchange.INFORMATIONAL_1:
            self.verify_hash(request)
            response_payloads = []
            delete_payload = request.get_payload(enums.Payload.DELETE_1)
            notify_payload = request.get_payload(enums.Payload.NOTIFY_1)
            if not request.payloads:
                pass
            elif delete_payload and delete_payload.protocol == enums.Protocol.IKE:
                self.state = State.DELETED
                self.sessions.pop(self.my_spi)
                response_payloads.append(delete_payload)
                message_id = request.message_id
            elif delete_payload:
                spis = []
                for spi in delete_payload.spis:
                    child_sa = next((x for x in self.child_sa if x.spi_out == spi), None)
                    if child_sa:
                        self.child_sa.remove(child_sa)
                        self.sessions.pop(child_sa.spi_in)
                        spis.append(child_sa.spi_in)
                response_payloads.append(message.PayloadDELETE_1(delete_payload.doi, delete_payload.protocol, spis))
                message_id = request.message_id
            elif notify_payload and notify_payload.notify == enums.Notify.ISAKMP_NTYPE_R_U_THERE:
                notify_payload.notify = enums.Notify.ISAKMP_NTYPE_R_U_THERE_ACK
                response_payloads.append(notify_payload)
                message_id = request.message_id
                message_id = None
            elif notify_payload and notify_payload.notify == enums.Notify.INITIAL_CONTACT_1:
                notify_payload.notify = enums.Notify.INITIAL_CONTACT_1
                response_payloads.append(notify_payload)
                message_id = request.message_id
            else:
                raise Exception(f'unhandled informational {request!r}')
            reply(self.response(enums.Exchange.INFORMATIONAL_1, response_payloads, message_id, crypto=self.crypto, hashmsg=True))
            #if notify_payload and notify_payload.notify == enums.Notify.INITIAL_CONTACT_1:
            #    reply(self.xauth_init())
        else:
            raise Exception(f'unhandled request {request!r}')

class IKEv2Session:
    def __init__(self, args, sessions, peer_spi):
        self.args = args
        self.sessions = sessions
        self.my_spi = os.urandom(8)
        self.peer_spi = peer_spi
        self.peer_msgid = 0
        self.my_crypto = None
        self.peer_crypto = None
        self.my_nonce = os.urandom(random.randrange(16, 256))
        self.peer_nonce = None
        self.state = State.INITIAL
        self.request_data = None
        self.response_data = None
        self.child_sa = []
        self.sessions[self.my_spi] = self
    def create_key(self, ike_proposal, shared_secret, old_sk_d=None):
        prf = crypto.Prf(ike_proposal.get_transform(enums.Transform.PRF).id)
        integ = crypto.Integrity(ike_proposal.get_transform(enums.Transform.INTEG).id)
        cipher = crypto.Cipher(ike_proposal.get_transform(enums.Transform.ENCR).id,
                               ike_proposal.get_transform(enums.Transform.ENCR).keylen)
        if not old_sk_d:
            skeyseed = prf.prf(self.peer_nonce+self.my_nonce, shared_secret)
        else:
            skeyseed = prf.prf(old_sk_d, shared_secret+self.peer_nonce+self.my_nonce)
        keymat = prf.prfplus(skeyseed, self.peer_nonce+self.my_nonce+self.peer_spi+self.my_spi,
                             prf.key_size*3+integ.key_size*2+cipher.key_size*2)
        self.sk_d, sk_ai, sk_ar, sk_ei, sk_er, sk_pi, sk_pr = struct.unpack(
            '>{0}s{1}s{1}s{2}s{2}s{0}s{0}s'.format(prf.key_size, integ.key_size, cipher.key_size), keymat)
        self.my_crypto = crypto.Crypto(cipher, sk_er, integ, sk_ar, prf, sk_pr)
        self.peer_crypto = crypto.Crypto(cipher, sk_ei, integ, sk_ai, prf, sk_pi)
    def create_child_key(self, child_proposal, nonce_i, nonce_r):
        integ = crypto.Integrity(child_proposal.get_transform(enums.Transform.INTEG).id)
        cipher = crypto.Cipher(child_proposal.get_transform(enums.Transform.ENCR).id,
                               child_proposal.get_transform(enums.Transform.ENCR).keylen)
        keymat = self.my_crypto.prf.prfplus(self.sk_d, nonce_i+nonce_r, 2*integ.key_size+2*cipher.key_size)
        sk_ei, sk_ai, sk_er, sk_ar = struct.unpack('>{0}s{1}s{0}s{1}s'.format(cipher.key_size, integ.key_size), keymat)
        crypto_in = crypto.Crypto(cipher, sk_ei, integ, sk_ai)
        crypto_out = crypto.Crypto(cipher, sk_er, integ, sk_ar)
        child_sa = ChildSa(os.urandom(4), child_proposal.spi, crypto_in, crypto_out)
        self.child_sa.append(child_sa)
        self.sessions[child_sa.spi_in] = child_sa
        return child_sa
    def auth_data(self, message_data, nonce, payload, sk_p):
        prf = self.peer_crypto.prf.prf
        return prf(prf(self.args.passwd.encode(), b'Key Pad for IKEv2'), message_data+nonce+prf(sk_p, payload.to_bytes()))
    def response(self, exchange, payloads, *, crypto=None):
        response = message.Message(self.peer_spi, self.my_spi, 0x20, exchange,
                enums.MsgFlag.Response, self.peer_msgid, payloads)
        #print(repr(response))
        self.peer_msgid += 1
        self.response_data = response.to_bytes(crypto=crypto)
        return self.response_data
    def process(self, request, stream, reply):
        if request.message_id == self.peer_msgid - 1:
            reply(self.response_data)
            return
        elif request.message_id != self.peer_msgid:
            return
        request.parse_payloads(stream, crypto=self.peer_crypto)
        print(repr(request))
        if request.exchange == enums.Exchange.IKE_SA_INIT:
            assert self.state == State.INITIAL
            self.peer_nonce = request.get_payload(enums.Payload.NONCE).nonce
            chosen_proposal = request.get_payload(enums.Payload.SA).get_proposal(enums.EncrId.ENCR_AES_CBC)
            payload_ke = request.get_payload(enums.Payload.KE)
            public_key, shared_secret = crypto.DiffieHellman(payload_ke.dh_group, payload_ke.ke_data)
            self.create_key(chosen_proposal, shared_secret)
            response_payloads = [ message.PayloadSA([chosen_proposal]),
                                  message.PayloadNONCE(self.my_nonce),
                                  message.PayloadKE(payload_ke.dh_group, public_key),
                                  message.PayloadNOTIFY(0, enums.Notify.NAT_DETECTION_DESTINATION_IP, b'', os.urandom(20)),
                                  message.PayloadNOTIFY(0, enums.Notify.NAT_DETECTION_SOURCE_IP, b'', os.urandom(20)) ]
            reply(self.response(enums.Exchange.IKE_SA_INIT, response_payloads))
            self.state = State.SA_SENT
            self.request_data = stream.getvalue()
        elif request.exchange == enums.Exchange.IKE_AUTH:
            assert self.state == State.SA_SENT
            request_payload_idi = request.get_payload(enums.Payload.IDi)
            request_payload_auth = request.get_payload(enums.Payload.AUTH)
            if request_payload_auth is None:
                EAP = True
                raise Exception('EAP not supported')
            else:
                EAP = False
                auth_data = self.auth_data(self.request_data, self.my_nonce, request_payload_idi, self.peer_crypto.sk_p)
                assert auth_data == request_payload_auth.auth_data, 'Authentication Failed'
            chosen_child_proposal = request.get_payload(enums.Payload.SA).get_proposal(enums.EncrId.ENCR_AES_CBC)
            child_sa = self.create_child_key(chosen_child_proposal, self.peer_nonce, self.my_nonce)
            chosen_child_proposal.spi = child_sa.spi_in
            response_payload_idr = message.PayloadIDr(enums.IDType.ID_FQDN, f'{__title__}-{__version__}'.encode())
            auth_data = self.auth_data(self.response_data, self.peer_nonce, response_payload_idr, self.my_crypto.sk_p)

            response_payloads = [ message.PayloadSA([chosen_child_proposal]),
                                  request.get_payload(enums.Payload.TSi),
                                  request.get_payload(enums.Payload.TSr),
                                  response_payload_idr,
                                  message.PayloadAUTH(enums.AuthMethod.PSK, auth_data) ]
            if request.get_payload(enums.Payload.CP):
                attrs = { enums.CPAttrType.INTERNAL_IP4_ADDRESS: ipaddress.ip_address('1.0.0.1').packed,
                          enums.CPAttrType.INTERNAL_IP4_DNS: ipaddress.ip_address(self.args.dns).packed, }
                response_payloads.append(message.PayloadCP(enums.CFGType.CFG_REPLY, attrs))
            reply(self.response(enums.Exchange.IKE_AUTH, response_payloads, crypto=self.my_crypto))
            self.state = State.ESTABLISHED
        elif request.exchange == enums.Exchange.INFORMATIONAL:
            assert self.state == State.ESTABLISHED
            response_payloads = []
            delete_payload = request.get_payload(enums.Payload.DELETE)
            if not request.payloads:
                pass
            elif delete_payload and delete_payload.protocol == enums.Protocol.IKE:
                self.state = State.DELETED
                self.sessions.pop(self.my_spi)
                for child_sa in self.child_sa:
                    self.sessions.pop(child_sa.spi_in)
                self.child_sa = []
                response_payloads.append(delete_payload)
            elif delete_payload:
                spis = []
                for spi in delete_payload.spis:
                    child_sa = next((x for x in self.child_sa if x.spi_out == spi), None)
                    if child_sa:
                        self.child_sa.remove(child_sa)
                        self.sessions.pop(child_sa.spi_in)
                        spis.append(child_sa.spi_in)
                response_payloads.append(message.PayloadDELETE(delete_payload.protocol, spis))
            else:
                raise Exception(f'unhandled informational {request!r}')
            reply(self.response(enums.Exchange.INFORMATIONAL, response_payloads, crypto=self.my_crypto))
        elif request.exchange == enums.Exchange.CREATE_CHILD_SA:
            assert self.state == State.ESTABLISHED
            chosen_proposal = request.get_payload(enums.Payload.SA).get_proposal(enums.EncrId.ENCR_AES_CBC)
            if chosen_proposal.protocol != enums.Protocol.IKE:
                payload_notify = next((i for i in request.get_payloads(enums.Payload.NOTIFY) if i.notify==enums.Notify.REKEY_SA), None)
                if not payload_notify:
                    raise Exception(f'unhandled protocol {chosen_proposal.protocol} {request!r}')
                old_child_sa = next(i for i in self.child_sa if i.spi_out == payload_notify.spi)
                peer_nonce = request.get_payload(enums.Payload.NONCE).nonce
                my_nonce = os.urandom(random.randrange(16, 256))
                child_sa = self.create_child_key(chosen_proposal, peer_nonce, my_nonce)
                chosen_proposal.spi = child_sa.spi_in
                old_child_sa.child = child_sa
                response_payloads = [ message.PayloadNOTIFY(chosen_proposal.protocol, enums.Notify.REKEY_SA, old_child_sa.spi_in, b''),
                                      message.PayloadNONCE(my_nonce),
                                      message.PayloadSA([chosen_proposal]),
                                      request.get_payload(enums.Payload.TSi),
                                      request.get_payload(enums.Payload.TSr) ]
            else:
                child = IKEv2Session(self.args, self.sessions, chosen_proposal.spi)
                child.state = State.ESTABLISHED
                child.peer_nonce = request.get_payload(enums.Payload.NONCE).nonce
                child.child_sa = self.child_sa
                self.child_sa = []
                payload_ke = request.get_payload(enums.Payload.KE)
                public_key, shared_secret = crypto.DiffieHellman(payload_ke.dh_group, payload_ke.ke_data)
                chosen_proposal.spi = child.my_spi
                child.create_key(chosen_proposal, shared_secret, self.sk_d)
                response_payloads = [ message.PayloadSA([chosen_proposal]),
                                      message.PayloadNONCE(child.my_nonce),
                                      message.PayloadKE(payload_ke.dh_group, public_key) ]
            reply(self.response(enums.Exchange.CREATE_CHILD_SA, response_payloads, crypto=self.my_crypto))
        else:
            raise Exception(f'unhandled request {request!r}')

IKE_HEADER = b'\x00\x00\x00\x00'

class IKE_500(asyncio.DatagramProtocol):
    def __init__(self, args, sessions):
        self.args = args
        self.sessions = sessions
    def connection_made(self, transport):
        self.transport = transport
    def datagram_received(self, data, addr, *, response_header=b''):
        stream = io.BytesIO(data)
        request = message.Message.parse(stream)
        if request.exchange == enums.Exchange.IKE_SA_INIT:
            session = IKEv2Session(self.args, self.sessions, request.spi_i)
        elif request.exchange == enums.Exchange.IDENTITY_1 and request.spi_r == bytes(8):
            session = IKEv1Session(self.args, self.sessions, request.spi_i, addr[0])
        else:
            session = self.sessions.get(request.spi_r)
            if session is None:
                return
        session.process(request, stream, lambda response: self.transport.sendto(response_header+response, addr))

class SPE_4500(IKE_500):
    def __init__(self, args, sessions):
        IKE_500.__init__(self, args, sessions)
        self.tcp_stack = {}
        self.dnscache = dns.DNSCache()
    def datagram_received(self, data, addr):
        spi = data[:4]
        if spi == b'\xff':
            self.transport.sendto(b'\xff', addr)
        elif spi == IKE_HEADER:
            IKE_500.datagram_received(self, data[4:], addr, response_header=IKE_HEADER)
        elif spi in self.sessions:
            seqnum = int.from_bytes(data[4:8], 'big')
            sa = self.sessions[spi]
            if seqnum < sa.msgid_in or seqnum in sa.msgwin_in:
                return
            if sa.msgid_in == 1 and sa.crypto_in.integrity.hasher is hashlib.sha256 and (len(data)-8)%16 == 12:
                # HMAC-SHA2-256-96 fix
                sa.crypto_in.integrity.hash_size = 12
                sa.crypto_out.integrity.hash_size = 12
            sa.crypto_in.verify_checksum(data)
            if seqnum > sa.msgid_in + 65536:
                sa.incr_msgid_in()
            if seqnum == sa.msgid_in:
                sa.incr_msgid_in()
            else:
                sa.msgwin_in.add(seqnum)
            header, data = sa.crypto_in.decrypt_esp(data[8:])
            def reply(data):
                nonlocal sa
                while sa and sa.spi_in not in self.sessions:
                    sa = sa.child
                if not sa:
                    return False
                encrypted = bytearray(sa.crypto_out.encrypt_esp(header, data))
                encrypted[0:0] = sa.spi_out + sa.msgid_out.to_bytes(4, 'big')
                sa.crypto_out.add_checksum(encrypted)
                sa.msgid_out += 1
                self.transport.sendto(encrypted, addr)
                return True
            if header == enums.IpProto.IPV4:
                proto, src_ip, dst_ip, ip_body = ip.parse_ipv4(data)
                dst_name = self.dnscache.ip2domain(str(dst_ip))
                if proto == enums.IpProto.UDP:
                    src_port, dst_port, udp_body = ip.parse_udp(ip_body)
                    if dst_port == 53:
                        try:
                            record = dns.DNSRecord.unpack(udp_body)
                            answer = self.dnscache.query(record)
                            print(f'IPv4 DNS -> {dst_name}:{dst_port} Query={record.q.qname}{" (Cached)" if answer else ""}')
                            if answer:
                                ip_body = ip.make_udp(dst_port, src_port, answer.pack())
                                data = ip.make_ipv4(proto, dst_ip, src_ip, ip_body)
                                reply(data)
                                return
                        except Exception as e:
                            print(e)
                    else:
                        print(f'IPv4 UDP -> {dst_name}:{dst_port} Length={len(udp_body)}')
                    def udp_reply(udp_body):
                        #print(f'IPv4 UDP Reply {dst_ip}:{dst_port} -> {src_ip}:{src_port}', result)
                        if dst_port == 53:
                            record = dns.DNSRecord.unpack(udp_body)
                            if not self.args.nocache:
                                self.dnscache.answer(record)
                            print(f'IPv4 DNS <- {dst_name}:{dst_port} Answer=['+' '.join(f'{r.rname}->{r.rdata}' for r in record.rr)+']')
                        else:
                            print(f'IPv4 UDP <- {dst_name}:{dst_port} Length={len(udp_body)}')
                        ip_body = ip.make_udp(dst_port, src_port, udp_body)
                        data = ip.make_ipv4(proto, dst_ip, src_ip, ip_body)
                        reply(data)
                    asyncio.ensure_future(self.args.urserver.udp_sendto(dst_name, dst_port, udp_body, udp_reply, (str(src_ip), src_port)))
                elif proto == enums.IpProto.TCP:
                    src_port, dst_port, flag, tcp_body = ip.parse_tcp(ip_body)
                    #else:
                    #    print(f'IPv4 TCP {src_ip}:{src_port} -> {dst_ip}:{dst_port}', ip_body)
                    key = (addr[0], src_port)
                    if key not in self.tcp_stack:
                        if flag & 2:
                            print(f'IPv4 TCP -> {dst_name}:{dst_port} Connect')
                        for spi, tcp in list(self.tcp_stack.items()):
                            if tcp.obsolete():
                                self.tcp_stack.pop(spi)
                        self.tcp_stack[key] = tcp = ip.TCPStack(src_ip, src_port, dst_ip, dst_name, dst_port, reply, self.args.rserver)
                    else:
                        tcp = self.tcp_stack[key]
                    tcp.parse(ip_body)
                elif proto == enums.IpProto.ICMP:
                    icmptp, code, icmp_body = ip.parse_icmp(ip_body)
                    if icmptp == 0:
                        tid, seq = struct.unpack('>HH', ip_body[4:8])
                        print(f'IPv4 PING -> {dst_name} Id={tid} Seq={seq} Data={icmp_body}')
                    elif icmptp == 8:
                        tid, seq = struct.unpack('>HH', ip_body[4:8])
                        print(f'IPv4 ECHO -> {dst_name} Id={tid} Seq={seq} Data={icmp_body}')
                        # NEED ROOT PRIVILEGE TO SEND ICMP PACKET
                        # a = socket.socket(socket.AF_INET, socket.SOCK_RAW, proto)
                        # a.sendto(icmp_body, (dst_name, 1))
                        # a.close()
                    elif icmptp == 3 and code == 3:
                        eproto, esrc_ip, edst_ip, eip_body = ip.parse_ipv4(icmp_body)
                        eport = int.from_bytes(eip_body[2:4], 'big')
                        print(f'IPv4 ICMP -> {dst_name} {eproto.name} :{eport} Denied')
                    else:
                        print(f'IPv4 ICMP -> {dst_name} Data={ip_body}')
                else:
                    print(f'IPv4 {enums.IpProto(proto).name} -> {dst_name} Data={data}')
            else:
                print(f'{enums.IpProto(header).name} Unhandled Protocol. Data={data}')
        else:
            print('unknown packet', data, addr)

DIRECT = pproxy.Connection('direct://')

def main():
    parser = argparse.ArgumentParser(description=__description__, epilog=f'Online help: <{__url__}>')
    parser.add_argument('-r', dest='rserver', default=DIRECT, type=pproxy.Connection, help='tcp remote server uri (default: direct)')
    parser.add_argument('-ur', dest='urserver', default=DIRECT, type=pproxy.Connection, help='udp remote server uri (default: direct)')
    parser.add_argument('-p', dest='passwd', default='test', help='password (default: test)')
    parser.add_argument('-dns', dest='dns', default='1.1.1.1', help='dns server (default: 1.1.1.1)')
    parser.add_argument('-nc', dest='nocache', default=None, action='store_true', help='do not cache dns (default: off)')
    parser.add_argument('-v', dest='v', action='count', help='print verbose output')
    parser.add_argument('--version', action='version', version=f'{__title__} {__version__}')
    args = parser.parse_args()
    loop = asyncio.get_event_loop()
    sessions = {}
    transport1, _ = loop.run_until_complete(loop.create_datagram_endpoint(lambda: IKE_500(args, sessions), ('0.0.0.0', 500)))
    transport2, _ = loop.run_until_complete(loop.create_datagram_endpoint(lambda: SPE_4500(args, sessions), ('0.0.0.0', 4500)))
    print('Serving on UDP :500 :4500...')
    try:
        loop.run_forever()
    except KeyboardInterrupt:
        print('exit')
    for task in asyncio.Task.all_tasks():
        task.cancel()
    transport1.close()
    transport2.close()
    loop.run_until_complete(loop.shutdown_asyncgens())
    loop.close()

if __name__ == '__main__':
    main()
