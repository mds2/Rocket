# -*- coding: utf-8 -*-

# Import System Modules
import os
import sys
import socket
import logging
from email.utils import formatdate
from wsgiref.util import FileWrapper
from types import GeneratorType as genType
# Import 3rd Party Modules
### None ###
# Import Custom Modules
from .. import HTTP_SERVER_NAME, b, u, BUF_SIZE
from ..worker import Worker, ChunkedReader

# Define Constants
STATUS_LINE = 'Status: {0}\r\n'
HEADER_LINE = '{0}: {1}\r\n'
NEWLINE = b('\r\n')
HEADER_RESPONSE = '''HTTP/1.1 {0}\r\n{1}\r\n'''

# Setup Logging
log = logging.getLogger('Rocket.WSGI')


class WSGIWorker(Worker):
    def __init__(self):
        """Builds some instance variables that will last the life of the
        thread."""
        self.base_environ = dict(os.environ.items())
        self.base_environ.update({'SERVER_NAME': self.server_name,
                                  'SERVER_PORT': self.server_port,
                                  'wsgi.errors': sys.stderr,
                                  'wsgi.version': (1, 0),
                                  'wsgi.multithread': self.max_threads == 1,
                                  'wsgi.multiprocess': False,
                                  'wsgi.run_once': False,
                                  'wsgi.file_wrapper': FileWrapper
                                  })
        # Grab our application
        if isinstance(self.app_info, dict):
            self.app = self.app_info.get('wsgi_app', TestApp)
        else:
            self.app = TestApp

        Worker.__init__(self)

    def build_environ(self, sock_file, addr):
        """ Build the execution environment. """
        # Grab the request line
        d = sock_file.readline().strip()
        if not d:
            # Allow an extra NEWLINE at the beginner per HTTP 1.1 spec
            d = sock_file.readline().strip()

        if not d:
            raise socket.error('Client closed socket.')
        line_one = d.split(b(' '))

        # Grab the headers
        headers = dict()
        lower_headers = dict() # HTTP headers are not case sensitive
        l = sock_file.readline()
        while l.strip():
            try:
                # HTTP header values are latin-1 encoded
                l = u(l, 'latin-1').split(u(':'), 1)
                # HTTP header names are us-ascii encoded
                lname = u(u('HTTP_') + l[0].strip(), 'us-ascii')
                lval = l[-1].strip()
                headers.update({lname: lval})
            except UnicodeDecodeError:
                log.warning('Client sent invalid header: ' + l.__repr__())

            l = sock_file.readline()

        # Save an easily accessible set of headers
        self.lower_headers = dict([(k.lower(), v) for k,v in headers.items()])

        # Copy the Base Environment
        environ = dict(self.base_environ)

        # Add CGI Variables
        environ['REQUEST_METHOD'] = u(line_one[0])
        environ['PATH_INFO'] = u(line_one[1], 'latin-1')
        environ['SERVER_PROTOCOL'] = u(line_one[2])
        environ['SCRIPT_NAME'] = '' # Direct call WSGI does not need a name
        environ['REMOTE_ADDR'] = u(str(addr[0]))

        # Save the request method for later
        self.request_method = environ['REQUEST_METHOD'].upper()

        # Add Dynamic WSGI Variables
        environ['wsgi.url_scheme'] = u(line_one[2].split(b('/'))[0]).lower()
        if lower_headers.get('transfer_encoding', '').lower() == 'chunked':
            environ['wsgi.input'] = ChunkedReader(sock_file)
        else:
            environ['wsgi.input'] = sock_file

        # Add HTTP Headers
        environ.update(headers)

        # Finish WSGI Variables
        if b('?') in line_one[1]:
            environ['QUERY_STRING'] = line_one[1].split(b('?'), 1)[-1]
        if 'http_content_length' in self.lower_headers:
            environ['CONTENT_LENGTH'] = self.lower_headers['http_content_length']
        if 'http_content_type' in self.lower_headers:
            environ['content_type'] = self.lower_headers['http_content_type']

        return environ

    def send_headers(self, data, sections):
        # Before the first output, send the stored headers
        header_dict = dict([(x.lower(), y) for (x,y) in self.header_set])

        # Does the app want us to send output chunked?
        self.chunked = header_dict.get(u('transfer-encoding'), '').lower() == u('chunked')

        # Add a Date header if it's not there already
        if not b('date') in header_dict:
            self.header_set.append(('Date',
                                     formatdate(usegmt=True)))

        # Add a Server header if it's not there already
        if not b('server') in header_dict:
            self.header_set.append(('Server',
                                     HTTP_SERVER_NAME))

        if not b('content-length') in header_dict and not self.chunked:
            if sections == 1:
                # Add a Content-Length header if it's not there already
                self.header_set.append(('Content-Length', len(data)))
            else:
                # If they sent us more than one section, we blow chunks
                self.header_set.append(('Transfer-Encoding', 'Chunked'))
                self.chunked = True
                log.debug('Adding header...Transfer-Encoding: Chunked')

        # If the client or application asks to keep the connection
        # alive, do so unless data is chunked (which don't play well together)
        #
        # They don't "play well together" because the only way to flush a
        # socket buffer is to close the connection.  So in the case of a
        # chunked send, we always close the connection afterward.
        conn = header_dict.get(u('connection'), '').lower()
        client_conn = self.lower_headers.get(u('http_connection'), '').lower()
        if conn != u('close') and client_conn == u('keep-alive'):
            if self.chunked:
                if conn == u('keep-alive'):
                    # remove the keep-alive header
                    for h in ('Connection', 'CONNECTION', 'connection'):
                        try:
                            self.header_set.remove((u(h), conn))
                            break
                        except:
                            pass
                elif conn != u('close'):
                    self.header_set.append(('Connection', 'close'))
                    self.closeConnection = True
            else:
                self.header_set.append(('Connection', 'keep-alive'))
                self.closeConnection = False
        else:
            self.header_set.append(('Connection', 'close'))
            self.closeConnection = True

        # Build our output headers
        serialized_headers = ''.join([HEADER_LINE.format(k,v)
                                      for (k,v) in self.header_set])
        header_data = HEADER_RESPONSE.format(self.status,
                                             serialized_headers)

        # Send the headers
        log.debug('Sending Headers: {0}'.format(header_data.__repr__()))
        self.client.sendall(b(header_data))
        self.headers_sent = True

    def write_warning(self, data, sections=None):
        data.warning('WSGI app called write method directly.  This is obsolete'
                     ' behavior.')
        self.write(data, sections)

    def write(self, data, sections=None):
        """ Write the data to the output socket. """

        if self.error[0]:
            self.status = self.error[0]
            data = b(self.error[1])

        if not self.headers_sent:
            self.send_headers(data, sections)

        if self.request_method != u('HEAD'):
            if self.chunked:
                self.client.sendall(b('{0:x}\r\n'.format(len(data))))
                self.client.sendall(data + b('\r\n'))
            else:
                self.client.sendall(data)

    def start_response(self, status, response_headers, exc_info=None):
        """ Store the HTTP status and headers to be sent when self.write is
        called. """
        if exc_info:
            try:
                if self.headers_sent:
                    # Re-raise original exception if headers sent
                    # because this violates WSGI specification.
                    raise
            finally:
                exc_info = None
        elif self.header_set:
            raise AssertionError("Headers already set!")

        self.status = status
        # Make sure headers are bytes objects
        try:
            self.header_set = [(u(h[0], 'us-ascii').strip(),
                                u(h[1], 'latin-1').strip())
                for h in response_headers]
        except UnicodeDecodeError:
            self.error = ('500 Internal Server Error',
                          'HTTP Headers should be bytes')
            log.warning('Received non-byte HTTP Headers from client.')

        return self.write_warning

    def run_app(self, client, addr):
        self.header_set = []
        self.headers_sent = False
        self.error = (None, None)
        sections = None
        output = None

        # Build our file-like object
        sock_file = client.makefile('rb',BUF_SIZE)

        try:
            # Read the headers and build our WSGI environment
            environ = self.build_environ(sock_file, addr)

            # Send it to our WSGI application
            output = self.app(environ, self.start_response)
            if not hasattr(output, '__len__') and not isinstance(output, genType):
                self.error = ('500 Internal Server Error',
                              'WSGI applications must return a list or '
                              'generator type.')

            if hasattr(output, '__len__'):
                sections = len(output)

            for data in output:
                # Don't send headers until body appears
                if data:
                    self.write(data, sections)

            # If chunked, send our final chunk length
            if self.chunked:
                self.client.sendall(b('0\r\n'))

            # Send headers if the body was empty
            if not self.headers_sent:
                self.write('')

        finally:
            if hasattr(output,'close'):
                output.close()

            sock_file.close()

def TestApp(environ, start_response):
    status = '200 OK'
    data = [b('<h1>WSGI Works!</h1>')]
    response_headers = [('Content-type', 'text/html')]
    start_response(status, response_headers)
    return data
