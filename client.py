"""
Port the standard smtplib for use with tornado non-blocking application model 
"""

from tornado import iostream 
import socket
import logging 
logger = logging.getLogger(__name__)
from tornado import gen

try: 
    import ssl 
except: 
    _have_ssl = False
else:
    class SSLFakeFile(object):
        def __init__(self, sslobj):
            self.sslobj = sslobj 
        def readLine(self): 
            str = ""
            chr = None 
            while chr != "\n":
                chr = self.sslobj.read(1)
                if not chr: break 
                str += chr 
            return str 
             
    _have_ssl = True

errors = {
    501 : 'Syntax error in parameters or arguments'
}
CRCF = b'\r\n'

class SMTPAsync(object): 
    def __init__(self, host = '', port = 0, local_hostname = None):
        self.host = host 
        self.port = port 
        self.stream = None
        self.sock = None
        self.esmtp_features = {} 
        self.file = None
        self.done_esmtp = 0
        self.helo_resp = None 
        self.ehlo_resp = None
        if local_hostname: 
            self.local_hostname = local_hostname 
        else:
            fqdn = socket.getfqdn() 
            if '.' in fqdn: 
                logger.debug(fqdn)
                print(fqdn)
                self.local_hostname = bytes(fqdn, 'utf-8') 
            else: 
                addr = '127.0.0.1' 
                try: 
                    addr = socket.gethostbyname(socket.gethostname())
                except socket.gaierror: 
                    pass 
                self.local_hostname = '[%s]' % addr

    def has_extn(self, f): 
        return True 
            
    @gen.coroutine
    def _command(self, name, param = None): 
        if not self.stream: 
            raise StreamError("IOStream is not yet created")
        if self.stream.closed():
            raise StreamError("Stream is already closed")
        if self.stream.writing(): 
            # we can handle this case better than just throwing out 
            # an error 
            raise StreamError("Stream is occupied at the moment")

        # check if we really need to yield here      
        request = b''.join([name,b' ', param, CRCF]) if param else b''.join([name, CRCF])  
        self.stream.write(request)  
        response = yield self.stream.read_until(CRCF)

        # some commands such as ehlo returns a list of <code>-<subCommand>\r\n<code>-<subCommand> 
        # before the final status code. Ignore them for now
        while response[3] not in b' \r\n':
            response = yield self.stream.read_until(CRCF)

        code = int(response[0:3])
        if not 200 <= code < 300: 
            raise CommandError("Response code %s: %s" % (code, errors.get(code, response[3:])))
        return (code, response[3:])


    @gen.coroutine
    def connect(self, host = None, port = None):
        self.host = host if host else self.host 
        self.prot = port if port else self.port 
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM, 0) 
        self.stream = iostream.IOStream(self.sock)
        # seem like I don't need to put 'yield' here 
        # strange, I thought it should wait until connection established          
        # and then read data from stream. Maybe the stream api is forcing a wait on read_until call 
        self.stream.connect((self.host, self.port)) 
        response = yield self.stream.read_until(CRCF)
        code = int(response[0:3])
        if not 200 <= code < 300: 
            raise ConnectionError(response[3:])
        return (code, response[3:]) 

    @gen.coroutine
    def starttls(self, keyfile=None, certfile=None): 
        #TODO: check how to read the local computer name
        yield self.ehlo_or_helo_if_needed() 
        if not self.has_extn('starttls'): 
            raise SMTPError('STARTTLS extension not supported ') 

        code, msg = yield self._command(b'STARTTLS')
        if code == 220: 
            if not _have_ssl: 
                raise RuntimeError("No SSL support included in this Python ")
            self.sock = ssl.wrap_socket(self.sock, keyfile, certfile, do_handshake_on_connect= False) 
            # set blocking = True. Otherwise, exception will be thrown. I don't know how to make it non-blocking here yet 
            self.sock.do_handshake(True)
            self.file = SSLFakeFile(self.sock)
            self.helo_resp = None 
            self.ehlo_resp = None 
            self.esmtp_features = {}
            self.does_esmtp = 0 
        return (code, msg)

    @gen.coroutine
    def login(self, username, password):
        def encode_cram_md5(challenge, username, password): 
            challenge = base64.decodestring(challenge)
            response = username + " " + hmac.MAC(password, challenge).hexdigest() 
            return encode_base64(response, eol="")

        def encode_plain(user, password): 
            return encode_base64("\0%s\0%s" % (user, password), eol="")

        AUTH_PLAIN = "PLAIN"
        AUTH_CRAM_MD5 = "CRAM-MD5"
        AUTH_LOGIN = "LOGIN"

        yield self.ehlo_or_helo_if_needed()
        if not self.has_extn('auth'):
            raise SMTPError("SMTP Auth extension not supported by server ") 
        authlist = self.esmtp_features['auth'].split()
        preferred_auths = [AUTH_CRAM_MD5, AUTH_PLAIN, AUTH_LOGIN]
        
        authmethod = None
        for method in preferred_auths:
            if method in authlist:
                authmethod = method
                break

        if authmethod == AUTH_CRAM_MD5: 
            code, msg = yield self._command("AUTH", AUTH_CRAM_MD5)    
            if code == 503:
                #alr authenticated
                return (code, msg) 
            code, msg = yield self._command(encode_cram_md5(msg, username, password)) 
        elif authmethod == AUTH_PLAIN: 
            code, msg = yield self._command("AUTH", AUTH_PLAIN + " " + encode_plain(username, password))
        elif authmethod == AUTH_LOGIN: 
            code, msg = yield self._command("AUTH","%s %s" % (AUTH_LOGIN, encode_base64(user, eol="")))
            if code != 334: 
                raise SMTPAuthError() 
            code, msg = yield self._command(encode_base64(password, eol=""))
        elif authmethod is None: 
            raise SMTPError()
        if code not in (235, 503): 
            raise SMTPAuthError()
            
        return (code,msg) 
       
    @gen.coroutine
    def ehlo_or_helo_if_needed(self): 
        if not self.helo_resp and not self.ehlo_resp: 
            code, resp = yield self.ehlo()   
            if not (200<= code <300):
                code, resp = yield self.helo()
                if not (200 <= code < 300): 
                    raise ConnectionError("Hello error")


    @gen.coroutine
    def helo(self):
        raise NotImplementedError()

    @gen.coroutine
    def ehlo(self, name=''):        
        code, resp = yield self._command(b'ehlo',  name or self.local_hostname) 
        self.ehlo_resp = resp 
        if code == -1 and len (resp) == 0 : 
            self.close()
            raise ConnectionError("Server not connected")
        if code != 250:
            return (code, resp)
        self.does_esmtp =1
        #TODO: parse the response separately
        raise NotImplementedError()
        #resp = self.ehlo_resp.split('\n')




    @gen.coroutine
    def login(self,username, password): 
            yield self.stream.connect((self.host, self.port))
            data = yield self.stream.read_until(b'\r\n')
            logger.debug(data)

    def quit():
        pass 

class StreamError(Exception): 
    pass  
class CommandError(Exception): 
    pass
class ConnectionError(Exception):
    pass 
class SMTPError(Exception):
    pass 
class SMTPAuthError(Exception):
    pass
