#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
#

#
# Test the HTTP/1.x Adaptor
#

from __future__ import unicode_literals
from __future__ import division
from __future__ import absolute_import
from __future__ import print_function


import socket
import sys
from threading import Thread
try:
    from http.server import HTTPServer, BaseHTTPRequestHandler
    from http.client import HTTPConnection
    from http.client import HTTPException
except ImportError:
    from BaseHTTPServer import HTTPServer, BaseHTTPRequestHandler
    from httplib import HTTPConnection, HTTPException

from proton.handlers import MessagingHandler
from proton.reactor import Container
from system_test import TestCase, unittest, main_module, Qdrouterd
from system_test import TIMEOUT, Logger


class RequestMsg(object):
    """
    A 'hardcoded' HTTP request message.  This class writes its request
    message to the HTTPConnection.
    """
    def __init__(self, method, target, headers=None, body=None):
        self.method = method
        self.target = target
        self.headers = headers or {}
        self.body = body

    def send_request(self, conn):
        conn.putrequest(self.method, self.target)
        for key, value in self.headers.items():
            conn.putheader(key, value)
        conn.endheaders()
        if self.body:
            conn.send(self.body)


class ResponseMsg(object):
    """
    A 'hardcoded' HTTP response message.  This class writes its response
    message when called by the HTTPServer via the BaseHTTPRequestHandler
    """
    def __init__(self, status, version=None, reason=None,
                 headers=None, body=None, error=False):
        self.status = status
        self.version = version or "HTTP/1.1"
        self.reason = reason
        self.headers = headers or []
        self.body = body
        self.error = error

    def send_response(self, handler):
        if self.error:
            handler.send_error(self.status,
                               message=self.reason)
            return

        handler.send_response(self.status, self.reason)
        for key, value in self.headers.items():
            handler.send_header(key, value)
        handler.end_headers()

        if self.body:
            handler.wfile.write(self.body)
            handler.wfile.flush()


class ResponseValidator(object):
    """
    Validate a response as received by the HTTP client
    """
    def __init__(self, status=200, expect_headers=None, expect_body=None):
        if expect_headers is None:
            expect_headers = {}
        self.status = status
        self.expect_headers = expect_headers
        self.expect_body = expect_body

    def check_response(self, rsp):
        if self.status and rsp.status != self.status:
            raise Exception("Bad response code, expected %s got %s"
                            % (self.status, rsp.status))
        for key, value in self.expect_headers.items():
            if rsp.getheader(key) != value:
                raise Exception("Missing/bad header (%s), expected %s got %s"
                                % (key, value, rsp.getheader(key)))

        body = rsp.read()
        if (self.expect_body and self.expect_body != body):
            raise Exception("Bad response body expected %s got %s"
                            % (self.expect_body, body))
        return body


class RequestHandler(BaseHTTPRequestHandler):
    """
    Dispatches requests received by the HTTPServer based on the method
    """
    protocol_version = 'HTTP/1.1'

    def _execute_request(self, tests):
        for req, resp, val in tests:
            if req.target == self.path:
                self._consume_body()
                if not isinstance(resp, list):
                    resp = [resp]
                for r in resp:
                    r.send_response(self)
                return
        self.send_error(404, "Not Found")

    def do_GET(self):
        self._execute_request(self.server.system_tests["GET"])

    def do_HEAD(self):
        self._execute_request(self.server.system_tests["HEAD"])

    def do_POST(self):
        if self.path == "/SHUTDOWN":
            self.send_response(200, "OK")
            self.send_header("Content-Length", "13")
            self.end_headers()
            self.wfile.write(b'Server Closed')
            self.wfile.flush()
            self.close_connection = True
            self.server.server_killed = True
            return
        self._execute_request(self.server.system_tests["POST"])

    def do_PUT(self):
        self._execute_request(self.server.system_tests["PUT"])

    # these overrides just quiet the test output
    # comment them out to help debug:
    def log_request(self, code=None, size=None):
        pass

    def log_error(self, format=None, *args):
        pass

    def log_message(self, format=None, *args):
        pass

    def _consume_body(self):
        """
        Read the entire body off the rfile.  This must be done to allow
        multiple requests on the same socket
        """
        if self.command == 'HEAD':
            return b''

        for key, value in self.headers.items():
            if key.lower() == 'content-length':
                return self.rfile.read(int(value))

            if key.lower() == 'transfer-encoding'  \
               and 'chunked' in value.lower():
                body = b''
                while True:
                    header = self.rfile.readline().strip().split(b';')[0]
                    data = self.rfile.readline().rstrip()
                    body += data
                    if int(header) == 0:
                        break;
                return body
        return self.rfile.read()


class RequestHandler10(RequestHandler):
    """
    RequestHandler that forces the server to use HTTP version 1.0 semantics
    """
    protocol_version = 'HTTP/1.0'


class MyHTTPServer(HTTPServer):
    """
    Adds a switch to the HTTPServer to allow it to exit gracefully
    """
    def __init__(self, addr, handler_cls, testcases):
        self.system_tests = testcases
        HTTPServer.__init__(self, addr, handler_cls)


class TestServer(object):
    """
    A HTTPServer running in a separate thread
    """
    def __init__(self, server_port, client_port, tests, handler_cls=None):
        self._logger = Logger(title="TestServer", print_to_console=False)
        self._client_port = client_port
        self._server_addr = ("", server_port)
        self._server = MyHTTPServer(self._server_addr,
                                    handler_cls or RequestHandler,
                                    tests)
        self._server.allow_reuse_address = True
        self._thread = Thread(target=self._run)
        self._thread.daemon = True
        self._thread.start()

    def _run(self):
        self._logger.log("TestServer listening on %s:%s" % self._server_addr)
        try:
            self._server.server_killed = False
            while not self._server.server_killed:
                self._server.handle_request()
        except Exception as exc:
            self._logger.log("TestServer %s crash: %s" %
                             (self._server_addr, exc))
            raise
        self._logger.log("TestServer %s:%s closed" % self._server_addr)

    def wait(self, timeout=TIMEOUT):
        self._logger.log("TestServer %s:%s shutting down" % self._server_addr)
        if self._thread.is_alive():
            client = HTTPConnection("127.0.0.1:%s" % self._client_port,
                                    timeout=TIMEOUT)
            client.putrequest("POST", "/SHUTDOWN")
            client.putheader("Content-Length", "0")
            client.endheaders()
            client.getresponse()
            client.close()
            self._thread.join(timeout=TIMEOUT)
        if self._server:
            self._server.server_close()


class ThreadedTestClient(object):
    """
    An HTTP client running in a separate thread
    """
    def __init__(self, tests, port, repeat=1):
        self._conn_addr = ("127.0.0.1:%s" % port)
        self._tests = tests
        self._repeat = repeat
        self._logger = Logger(title="TestClient", print_to_console=False)
        self._thread = Thread(target=self._run)
        self._thread.daemon = True
        self.error = None
        self._thread.start()

    def _run(self):
        self._logger.log("TestClient connecting on %s" % self._conn_addr)
        client = HTTPConnection(self._conn_addr, timeout=TIMEOUT)
        for loop in range(self._repeat):
            for op, tests in self._tests.items():
                for req, _, val in tests:
                    self._logger.log("TestClient sending request")
                    req.send_request(client)
                    self._logger.log("TestClient getting response")
                    rsp = client.getresponse()
                    self._logger.log("TestClient response received")
                    if val:
                        try:
                            body = val.check_response(rsp)
                        except Exception as exc:
                            self._logger.log("TestClient response invalid: %s",
                                             str(exc))
                            self.error = "client failed: %s" % str(exc)
                            return

                        if req.method is "BODY" and body != b'':
                            self._logger.log("TestClient response invalid: %s",
                                             "body present!")
                            self.error = "error: body present!"
                            return

        client.close()
        self._logger.log("TestClient to %s closed" % self._conn_addr)

    def wait(self, timeout=TIMEOUT):
        self._thread.join(timeout=TIMEOUT)
        self._logger.log("TestClient %s shut down" % self._conn_addr)


class Http1AdaptorOneRouterTest(TestCase):
    """
    Test HTTP servers and clients attached to a standalone router
    """

    # HTTP/1.1 compliant test cases
    TESTS_11 = {
        #
        # GET
        #
        "GET": [
            (RequestMsg("GET", "/GET/error",
                        headers={"Content-Length": 0}),
             ResponseMsg(400, reason="Bad breath", error=True),
             ResponseValidator(status=400)),

            (RequestMsg("GET", "/GET/content_len",
                        headers={"Content-Length": "00"}),
             ResponseMsg(200, reason="OK",
                         headers={"Content-Length": 1,
                                  "Content-Type": "text/plain;charset=utf-8"},
                         body=b'?'),
             ResponseValidator(expect_headers={'Content-Length': '1'},
                               expect_body=b'?')),

            (RequestMsg("GET", "/GET/content_len_511",
                        headers={"Content-Length": 0}),
             ResponseMsg(200, reason="OK",
                         headers={"Content-Length": 511,
                                  "Content-Type": "text/plain;charset=utf-8"},
                         body=b'X' * 511),
             ResponseValidator(expect_headers={'Content-Length': '511'},
                               expect_body=b'X' * 511)),

            (RequestMsg("GET", "/GET/content_len_4096",
                        headers={"Content-Length": 0}),
             ResponseMsg(200, reason="OK",
                         headers={"Content-Length": 4096,
                                  "Content-Type": "text/plain;charset=utf-8"},
                         body=b'X' * 4096),
             ResponseValidator(expect_headers={'Content-Length': '4096'},
                               expect_body=b'X' * 4096)),

            (RequestMsg("GET", "/GET/chunked",
                        headers={"Content-Length": 0}),
             ResponseMsg(200, reason="OK",
                         headers={"transfer-encoding": "chunked",
                                  "Content-Type": "text/plain;charset=utf-8"},
                         # note: the chunk length does not count the trailing CRLF
                         body=b'16\r\n'
                         + b'Mary had a little pug \r\n'
                         + b'1b\r\n'
                         + b'Its name was "Skupper-Jack"\r\n'
                         + b'0\r\n'
                         + b'Optional: Trailer\r\n'
                         + b'Optional: Trailer\r\n'
                         + b'\r\n'),
             ResponseValidator(expect_headers={'transfer-encoding': 'chunked'},
                               expect_body=b'Mary had a little pug Its name was "Skupper-Jack"')),

            (RequestMsg("GET", "/GET/chunked_large",
                        headers={"Content-Length": 0}),
             ResponseMsg(200, reason="OK",
                         headers={"transfer-encoding": "chunked",
                                  "Content-Type": "text/plain;charset=utf-8"},
                         # note: the chunk length does not count the trailing CRLF
                         body=b'1\r\n'
                         + b'?\r\n'
                         + b'800\r\n'
                         + b'X' * 0x800 + b'\r\n'
                         + b'13\r\n'
                         + b'Y' * 0x13  + b'\r\n'
                         + b'0\r\n'
                         + b'Optional: Trailer\r\n'
                         + b'Optional: Trailer\r\n'
                         + b'\r\n'),
             ResponseValidator(expect_headers={'transfer-encoding': 'chunked'},
                               expect_body=b'?' + b'X' * 0x800 + b'Y' * 0x13)),

            (RequestMsg("GET", "/GET/info_content_len",
                        headers={"Content-Length": 0}),
             [ResponseMsg(100, reason="Continue",
                          headers={"Blab": 1, "Blob": "?"}),
              ResponseMsg(200, reason="OK",
                          headers={"Content-Length": 1,
                                   "Content-Type": "text/plain;charset=utf-8"},
                          body=b'?')],
             ResponseValidator(expect_headers={'Content-Type': "text/plain;charset=utf-8"},
                               expect_body=b'?')),

            # (RequestMsg("GET", "/GET/no_length",
            #             headers={"Content-Length": "0"}),
            #  ResponseMsg(200, reason="OK",
            #              headers={"Content-Type": "text/plain;charset=utf-8",
            #                       "connection": "close"
            #              },
            #              body=b'Hi! ' * 1024 + b'X'),
            #  ResponseValidator(expect_body=b'Hi! ' * 1024 + b'X')),
        ],
        #
        # HEAD
        #
        "HEAD": [
            (RequestMsg("HEAD", "/HEAD/test_01",
                        headers={"Content-Length": "0"}),
             ResponseMsg(200, headers={"App-Header-1": "Value 01",
                                       "Content-Length": "10",
                                       "App-Header-2": "Value 02"},
                         body=None),
             ResponseValidator(expect_headers={"App-Header-1": "Value 01",
                                               "Content-Length": "10",
                                               "App-Header-2": "Value 02"})
            ),
            (RequestMsg("HEAD", "/HEAD/test_02",
                        headers={"Content-Length": "0"}),
             ResponseMsg(200, headers={"App-Header-1": "Value 01",
                                       "Transfer-Encoding": "chunked",
                                       "App-Header-2": "Value 02"}),
             ResponseValidator(expect_headers={"App-Header-1": "Value 01",
                                               "Transfer-Encoding": "chunked",
                                               "App-Header-2": "Value 02"})),

            (RequestMsg("HEAD", "/HEAD/test_03",
                        headers={"Content-Length": "0"}),
             ResponseMsg(200, headers={"App-Header-3": "Value 03"}),
             ResponseValidator(expect_headers={"App-Header-3": "Value 03"})),
        ],
        #
        # POST
        #
        "POST": [
            (RequestMsg("POST", "/POST/test_01",
                        headers={"App-Header-1": "Value 01",
                                 "Content-Length": "19",
                                 "Content-Type": "application/x-www-form-urlencoded"},
                        body=b'one=1&two=2&three=3'),
             ResponseMsg(200, reason="OK",
                         headers={"Response-Header": "whatever",
                                  "Transfer-Encoding": "chunked"},
                         body=b'8\r\n'
                         + b'12345678\r\n'
                         + b'f\r\n'
                         + b'abcdefghijklmno\r\n'
                         + b'000\r\n'
                         + b'\r\n'),
             ResponseValidator(expect_body=b'12345678abcdefghijklmno')
            ),
            (RequestMsg("POST", "/POST/test_02",
                        headers={"App-Header-1": "Value 01",
                                 "Transfer-Encoding": "chunked"},
                        body=b'01\r\n'
                        + b'!\r\n'
                        + b'0\r\n\r\n'),
             ResponseMsg(200, reason="OK",
                         headers={"Response-Header": "whatever",
                                  "Content-Length": "9"},
                         body=b'Hi There!'),
             ResponseValidator(expect_body=b'Hi There!')
            ),
        ],
        #
        # PUT
        #
        "PUT": [
            (RequestMsg("PUT", "/PUT/test_01",
                        headers={"Put-Header-1": "Value 01",
                                 "Transfer-Encoding": "chunked",
                                 "Content-Type": "text/plain;charset=utf-8"},
                        body=b'80\r\n'
                        + b'$' * 0x80 + b'\r\n'
                        + b'0\r\n\r\n'),
             ResponseMsg(201, reason="Created",
                         headers={"Response-Header": "whatever",
                                  "Content-length": "3"},
                         body=b'ABC'),
             ResponseValidator(status=201, expect_body=b'ABC')
            ),

            (RequestMsg("PUT", "/PUT/test_02",
                        headers={"Put-Header-1": "Value 01",
                                 "Content-length": "0",
                                 "Content-Type": "text/plain;charset=utf-8"}),
             ResponseMsg(201, reason="Created",
                         headers={"Response-Header": "whatever",
                                  "Transfer-Encoding": "chunked"},
                         body=b'1\r\n$\r\n0\r\n\r\n'),
             ResponseValidator(status=201, expect_body=b'$')
            ),
        ]
    }

    # HTTP/1.0 compliant test cases  (no chunked, response length unspecified)
    TESTS_10 = {
        #
        # GET
        #
        "GET": [
            (RequestMsg("GET", "/GET/error",
                        headers={"Content-Length": 0}),
             ResponseMsg(400, reason="Bad breath", error=True),
             ResponseValidator(status=400)),

            (RequestMsg("GET", "/GET/content_len_511",
                        headers={"Content-Length": 0}),
             ResponseMsg(200, reason="OK",
                         headers={"Content-Length": 511,
                                  "Content-Type": "text/plain;charset=utf-8"},
                         body=b'X' * 511),
             ResponseValidator(expect_headers={'Content-Length': '511'},
                               expect_body=b'X' * 511)),

            (RequestMsg("GET", "/GET/content_len_4096",
                        headers={"Content-Length": 0}),
             ResponseMsg(200, reason="OK",
                         headers={"Content-Type": "text/plain;charset=utf-8"},
                         body=b'X' * 4096),
             ResponseValidator(expect_headers={"Content-Type": "text/plain;charset=utf-8"},
                               expect_body=b'X' * 4096)),

            (RequestMsg("GET", "/GET/info_content_len",
                        headers={"Content-Length": 0}),
             [ResponseMsg(100, reason="Continue",
                          headers={"Blab": 1, "Blob": "?"}),
              ResponseMsg(200, reason="OK",
                          headers={"Content-Type": "text/plain;charset=utf-8"},
                          body=b'?')],
             ResponseValidator(expect_headers={'Content-Type': "text/plain;charset=utf-8"},
                               expect_body=b'?')),

            # (RequestMsg("GET", "/GET/no_length",
            #             headers={"Content-Length": "0"}),
            #  ResponseMsg(200, reason="OK",
            #              headers={"Content-Type": "text/plain;charset=utf-8",
            #                       "connection": "close"
            #              },
            #              body=b'Hi! ' * 1024 + b'X'),
            #  ResponseValidator(expect_body=b'Hi! ' * 1024 + b'X')),
        ],
        #
        # HEAD
        #
        "HEAD": [
            (RequestMsg("HEAD", "/HEAD/test_01",
                        headers={"Content-Length": "0"}),
             ResponseMsg(200, headers={"App-Header-1": "Value 01",
                                       "Content-Length": "10",
                                       "App-Header-2": "Value 02"},
                         body=None),
             ResponseValidator(expect_headers={"App-Header-1": "Value 01",
                                               "Content-Length": "10",
                                               "App-Header-2": "Value 02"})
            ),

            (RequestMsg("HEAD", "/HEAD/test_03",
                        headers={"Content-Length": "0"}),
             ResponseMsg(200, headers={"App-Header-3": "Value 03"}),
             ResponseValidator(expect_headers={"App-Header-3": "Value 03"})),
        ],
        #
        # POST
        #
        "POST": [
            (RequestMsg("POST", "/POST/test_01",
                        headers={"App-Header-1": "Value 01",
                                 "Content-Length": "19",
                                 "Content-Type": "application/x-www-form-urlencoded"},
                        body=b'one=1&two=2&three=3'),
             ResponseMsg(200, reason="OK",
                         headers={"Response-Header": "whatever"},
                         body=b'12345678abcdefghijklmno'),
             ResponseValidator(expect_body=b'12345678abcdefghijklmno')
            ),
            (RequestMsg("POST", "/POST/test_02",
                        headers={"App-Header-1": "Value 01",
                                 "Content-Length": "5"},
                        body=b'01234'),
             ResponseMsg(200, reason="OK",
                         headers={"Response-Header": "whatever",
                                  "Content-Length": "9"},
                         body=b'Hi There!'),
             ResponseValidator(expect_body=b'Hi There!')
            ),
        ],
        #
        # PUT
        #
        "PUT": [
            (RequestMsg("PUT", "/PUT/test_01",
                        headers={"Put-Header-1": "Value 01",
                                 "Content-Length": "513",
                                 "Content-Type": "text/plain;charset=utf-8"},
                        body=b'$' * 513),
             ResponseMsg(201, reason="Created",
                         headers={"Response-Header": "whatever",
                                  "Content-length": "3"},
                         body=b'ABC'),
             ResponseValidator(status=201, expect_body=b'ABC')
            ),

            (RequestMsg("PUT", "/PUT/test_02",
                        headers={"Put-Header-1": "Value 01",
                                 "Content-length": "0",
                                 "Content-Type": "text/plain;charset=utf-8"}),
             ResponseMsg(201, reason="Created",
                         headers={"Response-Header": "whatever"},
                         body=b'No Content Length'),
             ResponseValidator(status=201, expect_body=b'No Content Length')
            ),
        ]
    }


    @classmethod
    def setUpClass(cls):
        """Start a router"""
        super(Http1AdaptorOneRouterTest, cls).setUpClass()

        def router(name, mode, extra):
            config = [
                ('router', {'mode': mode,
                            'id': name,
                            'allowUnsettledMulticast': 'yes'}),
                ('listener', {'role': 'normal',
                              'port': cls.tester.get_port()}),
                ('address', {'prefix': 'closest',   'distribution': 'closest'}),
                ('address', {'prefix': 'multicast', 'distribution': 'multicast'}),
            ]

            if extra:
                config.extend(extra)
            config = Qdrouterd.Config(config)
            cls.routers.append(cls.tester.qdrouterd(name, config, wait=True))
            return cls.routers[-1]

        # configuration:
        #  One interior router, two servers (one running as HTTP/1.0)
        #
        #  +----------------+
        #  |     INT.A      |
        #  +----------------+
        #      ^         ^
        #      |         |
        #      V         V
        #  <clients>  <servers>

        cls.routers = []
        #cls.http_server11_port = cls.tester.get_port()
        #cls.http_server10_port = cls.tester.get_port()
        #cls.http_listener11_port = cls.tester.get_port()
        #cls.http_listener10_port = cls.tester.get_port()
        cls.http_server11_port = 9090
        cls.http_listener11_port = 8080
        cls.http_server10_port = 9091
        cls.http_listener10_port = 8081

        router('INT.A', 'standalone',
               [('httpConnector', {'port': cls.http_server11_port,
                                   'protocolVersion': 'HTTP1',
                                   'address': 'testServer11'}),
                ('httpConnector', {'port': cls.http_server10_port,
                                   'protocolVersion': 'HTTP1',
                                   'address': 'testServer10'}),
                ('httpListener', {'port': cls.http_listener11_port,
                                  'protocolVersion': 'HTTP1',
                                  'address': 'testServer11'}),
                ('httpListener', {'port': cls.http_listener10_port,
                                  'protocolVersion': 'HTTP1',
                                  'address': 'testServer10'})
               ])

        cls.INT_A = cls.routers[0]
        cls.INT_A.listener = cls.INT_A.addresses[0]

        cls.http11_server = TestServer(server_port=cls.http_server11_port,
                                       client_port=cls.http_listener11_port,
                                       tests=cls.TESTS_11)
        cls.http10_server = TestServer(server_port=cls.http_server10_port,
                                       client_port=cls.http_listener10_port,
                                       tests=cls.TESTS_10,
                                       handler_cls=RequestHandler10)
        cls.INT_A.wait_connectors()

    @classmethod
    def tearDownClass(cls):
        if cls.http11_server:
            cls.http11_server.wait()
        if cls.http10_server:
            cls.http10_server.wait()
        super(Http1AdaptorOneRouterTest, cls).tearDownClass()

    def _do_request(self, client, tests):
        for req, _, val in tests:
            req.send_request(client)
            rsp = client.getresponse()
            try:
                body = val.check_response(rsp)
            except Exception as exc:
                self.fail("request failed:  %s" % str(exc))

            if req.method is "BODY":
                self.assertEqual(b'', body)

    def test_001_get(self):
        client = HTTPConnection("127.0.0.1:%s" % self.http_listener11_port,
                                timeout=TIMEOUT)
        self._do_request(client, self.TESTS_11["GET"])
        client.close()

    def test_002_head(self):
        client = HTTPConnection("127.0.0.1:%s" % self.http_listener11_port,
                                timeout=TIMEOUT)
        self._do_request(client, self.TESTS_11["HEAD"])
        client.close()

    def test_003_post(self):
        client = HTTPConnection("127.0.0.1:%s" % self.http_listener11_port,
                                timeout=TIMEOUT)
        self._do_request(client, self.TESTS_11["POST"])
        client.close()

    def test_004_put(self):
        client = HTTPConnection("127.0.0.1:%s" % self.http_listener11_port,
                                timeout=TIMEOUT)
        self._do_request(client, self.TESTS_11["PUT"])
        client.close()

    def test_005_get_10(self):
        client = HTTPConnection("127.0.0.1:%s" % self.http_listener10_port,
                                timeout=TIMEOUT)
        self._do_request(client, self.TESTS_10["GET"])
        client.close()

    def test_006_head_10(self):
        client = HTTPConnection("127.0.0.1:%s" % self.http_listener10_port,
                                timeout=TIMEOUT)
        self._do_request(client, self.TESTS_10["HEAD"])
        client.close()

    def test_007_post_10(self):
        client = HTTPConnection("127.0.0.1:%s" % self.http_listener10_port,
                                timeout=TIMEOUT)
        self._do_request(client, self.TESTS_10["POST"])
        client.close()

    def test_008_put_10(self):
        client = HTTPConnection("127.0.0.1:%s" % self.http_listener10_port,
                                timeout=TIMEOUT)
        self._do_request(client, self.TESTS_10["PUT"])
        client.close()


class Http1AdaptorInteriorTest(TestCase):
    """
    Test an HTTP server connected to an interior router serving multiple HTTP
    clients
    """
    TESTS = {
        "PUT": [
            (RequestMsg("PUT", "/PUT/test",
                        headers={"Header-1": "Value",
                                 "Header-2": "Value",
                                 "Content-Length": "20",
                                 "Content-Type": "text/plain;charset=utf-8"},
                        body=b'!' * 20),
             ResponseMsg(201, reason="Created",
                         headers={"Response-Header": "data",
                                  "Content-Length": "0"}),
             ResponseValidator(status=201)
            )],

        "POST": [
            (RequestMsg("POST", "/POST/test",
                        headers={"Header-1": "X",
                                 "Content-Length": "11",
                                 "Content-Type": "application/x-www-form-urlencoded"},
                        body=b'one=1' + b'&two=2'),
             ResponseMsg(200, reason="OK",
                         headers={"Response-Header": "whatever",
                                  "Content-Length": 10},
                         body=b'0123456789'),
             ResponseValidator()
            )],

        "GET": [
            (RequestMsg("GET", "/GET/test",
                        headers={"Content-Length": "000"}),
             ResponseMsg(200, reason="OK",
                         headers={"Content-Length": "655",
                                  "Content-Type": "text/plain;charset=utf-8"},
                         body=b'?' * 655),
             ResponseValidator(expect_headers={'Content-Length': '655'},
                               expect_body=b'?' * 655)
            )],

        "PUT": [
            (RequestMsg("PUT", "/PUT/chunked",
                        headers={"Transfer-Encoding": "chunked",
                                 "Content-Type": "text/plain;charset=utf-8"},
                        body=b'16\r\n' + b'!' * 0x16 + b'\r\n'
                        + b'0\r\n\r\n'),
             ResponseMsg(204, reason="No Content",
                        headers={"Content-Length": "000"}),
             ResponseValidator(status=204)
            )],
    }

    @classmethod
    def setUpClass(cls):
        """Start a router"""
        super(Http1AdaptorInteriorTest, cls).setUpClass()

        def router(name, mode, extra):
            config = [
                ('router', {'mode': mode,
                            'id': name,
                            'allowUnsettledMulticast': 'yes'}),
                ('listener', {'role': 'normal',
                              'port': cls.tester.get_port()}),
                ('address', {'prefix': 'closest',   'distribution': 'closest'}),
                ('address', {'prefix': 'multicast', 'distribution': 'multicast'}),
            ]

            if extra:
                config.extend(extra)
            config = Qdrouterd.Config(config)
            cls.routers.append(cls.tester.qdrouterd(name, config, wait=True))
            return cls.routers[-1]

        # configuration:
        # one edge, one interior
        #
        #  +-------+    +---------+
        #  |  EA1  |<==>|  INT.A  |
        #  +-------+    +---------+
        #      ^             ^
        #      |             |
        #      V             V
        #  <clients>      <server>

        cls.routers = []
        cls.INTA_edge_port   = cls.tester.get_port()
        #cls.http_server_port = cls.tester.get_port()
        #cls.http_listener_port = cls.tester.get_port()
        cls.http_server_port = 9090
        cls.http_listener_port = 8080

        router('INT.A', 'interior',
               [('listener', {'role': 'edge', 'port': cls.INTA_edge_port}),
                ('httpConnector', {'port': cls.http_server_port,
                                   'protocolVersion': 'HTTP1',
                                   'address': 'testServer'})
               ])
        cls.INT_A = cls.routers[0]
        cls.INT_A.listener = cls.INT_A.addresses[0]

        router('EA1', 'edge',
               [('connector', {'name': 'uplink', 'role': 'edge',
                               'port': cls.INTA_edge_port}),
                ('httpListener', {'port': cls.http_listener_port,
                                  'protocolVersion': 'HTTP1',
                                  'address': 'testServer'})
               ])
        cls.EA1 = cls.routers[1]
        cls.EA1.listener = cls.EA1.addresses[0]

        cls.EA1.wait_connectors()
        cls.INT_A.wait_address('EA1')


    def test_01_load(self):
        """
        Test multiple clients running as fast as possible
        """
        server = TestServer(server_port=self.http_server_port,
                            client_port=self.http_listener_port,
                            tests=self.TESTS)

        clients = []
        for _ in range(5):
            clients.append(ThreadedTestClient(self.TESTS,
                                              self.http_listener_port,
                                              repeat=2))
        for client in clients:
            client.wait()
            self.assertIsNone(client.error)

        # send command to stop the server thread
        client = ThreadedTestClient({"POST": [(RequestMsg("POST",
                                                          "/SHUTDOWN",
                                                          {"Content-Length": "0"}),
                                               None,
                                               None)]},
                                    self.http_listener_port)
        client.wait()
        self.assertIsNone(client.error)

        server.wait()


if __name__ == '__main__':
    unittest.main(main_module())
