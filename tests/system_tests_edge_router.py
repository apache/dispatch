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

from __future__ import unicode_literals
from __future__ import division
from __future__ import absolute_import
from __future__ import print_function

from time import sleep

import unittest2 as unittest
from proton import Message, Timeout
from system_test import TestCase, Qdrouterd, main_module, TIMEOUT, MgmtMsgProxy
from system_test import AsyncTestReceiver
from system_test import AsyncTestSender
from system_tests_link_routes import ConnLinkRouteService
from test_broker import FakeService
from proton.handlers import MessagingHandler
from proton.reactor import Container, DynamicNodeProperties


class RouterTest(TestCase):

    inter_router_port = None

    @classmethod
    def setUpClass(cls):
        """Start a router"""
        super(RouterTest, cls).setUpClass()

        def router(name, mode, connection, extra=None):
            config = [
                ('router', {'mode': mode, 'id': name}),
                ('listener', {'port': cls.tester.get_port(), 'stripAnnotations': 'no'}),
                ('listener', {'port': cls.tester.get_port(), 'stripAnnotations': 'no', 'multiTenant': 'yes'}),
                ('listener', {'port': cls.tester.get_port(), 'stripAnnotations': 'no', 'role': 'route-container'}),
                ('linkRoute', {'prefix': '0.0.0.0/link', 'direction': 'in', 'containerId': 'LRC'}),
                ('linkRoute', {'prefix': '0.0.0.0/link', 'direction': 'out', 'containerId': 'LRC'}),
                ('autoLink', {'addr': '0.0.0.0/queue.waypoint', 'containerId': 'ALC', 'direction': 'in'}),
                ('autoLink', {'addr': '0.0.0.0/queue.waypoint', 'containerId': 'ALC', 'direction': 'out'}),
                ('address', {'prefix': 'closest', 'distribution': 'closest'}),
                ('address', {'prefix': 'spread', 'distribution': 'balanced'}),
                ('address', {'prefix': 'multicast', 'distribution': 'multicast'}),
                ('address', {'prefix': '0.0.0.0/queue', 'waypoint': 'yes'}),
                connection
            ]

            if extra:
                config.append(extra)
            config = Qdrouterd.Config(config)
            cls.routers.append(cls.tester.qdrouterd(name, config, wait=True))

        cls.routers = []

        inter_router_port = cls.tester.get_port()
        edge_port_A       = cls.tester.get_port()
        edge_port_B       = cls.tester.get_port()

        router('INT.A', 'interior', ('listener', {'role': 'inter-router', 'port': inter_router_port}),
               ('listener', {'role': 'edge', 'port': edge_port_A}))
        router('INT.B', 'interior', ('connector', {'name': 'connectorToA', 'role': 'inter-router', 'port': inter_router_port}),
               ('listener', {'role': 'edge', 'port': edge_port_B}))
        router('EA1',   'edge',     ('connector', {'name': 'edge', 'role': 'edge', 'port': edge_port_A}))
        router('EA2',   'edge',     ('connector', {'name': 'edge', 'role': 'edge', 'port': edge_port_A}))
        router('EB1',   'edge',     ('connector', {'name': 'edge', 'role': 'edge', 'port': edge_port_B}))
        router('EB2',   'edge',     ('connector', {'name': 'edge', 'role': 'edge', 'port': edge_port_B}))

        cls.routers[0].wait_router_connected('INT.B')
        cls.routers[1].wait_router_connected('INT.A')


    def test_01_connectivity_INTA_EA1(self):
        test = ConnectivityTest(self.routers[0].addresses[0],
                                self.routers[2].addresses[0],
                                'EA1')
        test.run()
        self.assertEqual(None, test.error)

    def test_02_connectivity_INTA_EA2(self):
        test = ConnectivityTest(self.routers[0].addresses[0],
                                self.routers[3].addresses[0],
                                'EA2')
        test.run()
        self.assertEqual(None, test.error)

    def test_03_connectivity_INTB_EB1(self):
        test = ConnectivityTest(self.routers[1].addresses[0],
                                self.routers[4].addresses[0],
                                'EB1')
        test.run()
        self.assertEqual(None, test.error)

    def test_04_connectivity_INTB_EB2(self):
        test = ConnectivityTest(self.routers[1].addresses[0],
                                self.routers[5].addresses[0],
                                'EB2')
        test.run()
        self.assertEqual(None, test.error)

    def test_05_dynamic_address_same_edge(self):
        test = DynamicAddressTest(self.routers[2].addresses[0],
                                  self.routers[2].addresses[0])
        test.run()
        self.assertEqual(None, test.error)

    def test_06_dynamic_address_interior_to_edge(self):
        test = DynamicAddressTest(self.routers[2].addresses[0],
                                  self.routers[0].addresses[0])
        test.run()
        self.assertEqual(None, test.error)

    def test_07_dynamic_address_edge_to_interior(self):
        test = DynamicAddressTest(self.routers[0].addresses[0],
                                  self.routers[2].addresses[0])
        test.run()
        self.assertEqual(None, test.error)

    def test_08_dynamic_address_edge_to_edge_one_interior(self):
        test = DynamicAddressTest(self.routers[2].addresses[0],
                                  self.routers[3].addresses[0])
        test.run()
        self.assertEqual(None, test.error)

    def test_09_dynamic_address_edge_to_edge_two_interior(self):
        test = DynamicAddressTest(self.routers[2].addresses[0],
                                  self.routers[4].addresses[0])
        test.run()
        self.assertEqual(None, test.error)

    def test_10_mobile_address_same_edge(self):
        test = MobileAddressTest(self.routers[2].addresses[0],
                                 self.routers[2].addresses[0],
                                 "test_10")
        test.run()
        self.assertEqual(None, test.error)

    def test_11_mobile_address_interior_to_edge(self):
        test = MobileAddressTest(self.routers[2].addresses[0],
                                 self.routers[0].addresses[0],
                                 "test_11")
        test.run()
        self.assertEqual(None, test.error)

    def test_12_mobile_address_edge_to_interior(self):
        self.skipTest("Temporarily disabled")
        test = MobileAddressTest(self.routers[0].addresses[0],
                                 self.routers[2].addresses[0],
                                 "test_12")
        test.run()
        self.assertEqual(None, test.error)

    def test_13_mobile_address_edge_to_edge_one_interior(self):
        self.skipTest("Temporarily disabled")
        test = MobileAddressTest(self.routers[2].addresses[0],
                                 self.routers[3].addresses[0],
                                 "test_13")
        test.run()
        self.assertEqual(None, test.error)

    def test_14_mobile_address_edge_to_edge_two_interior(self):
        self.skipTest("Temporarily disabled")
        test = MobileAddressTest(self.routers[2].addresses[0],
                                 self.routers[4].addresses[0],
                                 "test_14")
        test.run()
        self.assertEqual(None, test.error)


class LinkRouteProxyTest(TestCase):
    """
    Test edge router's ability to proxy configured and connection-scoped link
    routes into the interior
    """

    @classmethod
    def setUpClass(cls):
        """Start a router"""
        super(LinkRouteProxyTest, cls).setUpClass()

        def router(name, mode, extra):
            config = [
                ('router', {'mode': mode, 'id': name}),
                ('listener', {'role': 'normal', 'port': cls.tester.get_port()})
            ]

            if extra:
                config.extend(extra)
            config = Qdrouterd.Config(config)
            cls.routers.append(cls.tester.qdrouterd(name, config, wait=True))
            return cls.routers[-1]

        # configuration:
        # two edge routers connected via 2 interior routers.
        #
        #  +-------+    +---------+    +---------+    +-------+
        #  |  EA1  |<==>|  INT.A  |<==>|  INT.B  |<==>|  EB1  |
        #  +-------+    +---------+    +---------+    +-------+

        cls.routers = []

        router('INT.A', 'interior',
               [('listener', {'role': 'inter-router', 'port': cls.tester.get_port()})])
        cls.INT_A = cls.routers[0]

        router('INT.B', 'interior',
               [('connector', {'name': 'connectorToA', 'role': 'inter-router',
                               'port': cls.INT_A.ports[1]})])
        cls.INT_B = cls.routers[1]

        router('EA1', 'edge',
               [('listener', {'name': 'rc', 'role': 'route-container',
                              'port': cls.tester.get_port()}),
                ('connector', {'name': 'uplink', 'role': 'edge',
                               'port': cls.INT_A.ports[0]}),
                ('linkRoute', {'prefix': 'CfgLinkRoute1', 'containerId': 'FakeBroker', 'direction': 'in'}),
                ('linkRoute', {'prefix': 'CfgLinkRoute1', 'containerId': 'FakeBroker', 'direction': 'out'})])
        cls.EA1 = cls.routers[2]

        router('EB1', 'edge',
               [('connector', {'name': 'uplink', 'role': 'edge',
                               'port': cls.INT_B.ports[0]})])
        cls.EB1 = cls.routers[3]

        cls.INT_A.wait_router_connected('INT.B')
        cls.INT_B.wait_router_connected('INT.A')
        cls.EA1.wait_connectors()
        cls.EB1.wait_connectors()

        cls.CFG_LINK_ROUTE_TYPE = 'org.apache.qpid.dispatch.router.config.linkRoute'
        cls.CONN_LINK_ROUTE_TYPE = 'org.apache.qpid.dispatch.router.connection.linkRoute'
        cls.CONNECTOR_TYPE = 'org.apache.qpid.dispatch.connector'

    def _get_address(self, router, address):
        a_type = 'org.apache.qpid.dispatch.router.address'
        addrs = router.management.query(a_type).get_dicts()
        return list(filter(lambda a: a['name'].find(address) != -1,
                           addrs))

    def _test_traffic(self, sender, receiver, address, count=5):
        tr = AsyncTestReceiver(receiver, address)
        ts = AsyncTestSender(sender, address, count)
        ts.wait()  # wait until all sent
        for i in range(count):
            tr.queue.get(timeout=TIMEOUT)
        tr.stop()

    def test_link_route_proxy_configured(self):
        """
        Activate the configured link routes via a FakeService, verify proxies
        created by passing traffic from/to and interior router
        """

        fs = FakeService(self.EA1.addresses[1])
        self.INT_B.wait_address("CfgLinkRoute1")
        self._test_traffic(self.INT_B.addresses[0],
                           self.INT_B.addresses[0],
                           "CfgLinkRoute1/hi",
                           count=5)
        fs.join()
        self.assertEqual(5, fs.in_count)
        self.assertEqual(5, fs.out_count)

    def test_conn_link_route_proxy(self):
        """
        Test connection scoped link routes
        """
        fs = ConnLinkRouteService(self.EA1.addresses[1],
                                  container_id="FakeService",
                                  config = [("ConnLinkRoute1",
                                             {"pattern": "Conn/*/One",
                                              "direction": "out"}),
                                            ("ConnLinkRoute2",
                                             {"pattern": "Conn/*/One",
                                              "direction": "in"})])
        self.assertEqual(2, len(fs.values))

        self.INT_B.wait_address("Conn/*/One")
        self.assertEqual(2, len(self._get_address(self.INT_A, "Conn/*/One")))

        self._test_traffic(self.INT_B.addresses[0],
                           self.INT_A.addresses[0],
                           "Conn/BLAB/One",
                           count=5)
        fs.join()
        self.assertEqual(5, fs.in_count)
        self.assertEqual(5, fs.out_count)

        # the link route service connection is closed, verify delete
        self.assertEqual(0, len(self._get_address(self.INT_A, "Conn/*/One")))

    def test_interior_conn_lost(self):
        """
        What happens when the interior connection bounces?
        """
        config = Qdrouterd.Config([('router', {'mode': 'edge',
                                               'id': 'Edge1'}),
                                   ('listener', {'role': 'normal',
                                                 'port': self.tester.get_port()}),
                                   ('listener', {'name': 'rc',
                                                 'role': 'route-container',
                                                 'port': self.tester.get_port()}),
                                   ('linkRoute', {'pattern': 'Edge1/*',
                                                  'containerId': 'FakeBroker',
                                                  'direction': 'in'}),
                                   ('linkRoute', {'pattern': 'Edge1/*',
                                                  'containerId': 'FakeBroker',
                                                  'direction': 'out'})])
        er = self.tester.qdrouterd('Edge1', config, wait=True)

        # activate the link routes before the connection exists
        fs = FakeService(er.addresses[1])
        er.wait_address("Edge1/*")


        # create the connection to interior
        er_mgmt = er.management
        ctor = er_mgmt.create(type=self.CONNECTOR_TYPE,
                              name='toA',
                              attributes={'role': 'edge',
                                          'port': self.INT_A.ports[0]})
        self.INT_B.wait_address("Edge1/*")

        # delete it, and verify the routes are removed
        ctor.delete()
        while self._get_address(self.INT_B, "Edge1/*"):
            sleep(0.5)

        # now recreate and verify routes re-appear
        ctor = er_mgmt.create(type=self.CONNECTOR_TYPE,
                              name='toA',
                              attributes={'role': 'edge',
                                          'port': self.INT_A.ports[0]})
        self.INT_B.wait_address("Edge1/*")
        er.teardown()


    def test_thrashing_link_routes(self):
        """
        Rapidly add and delete link routes at the edge
        """

        # activate the pre-configured link routes
        ea1_mgmt = self.EA1.management
        fs = FakeService(self.EA1.addresses[1])
        self.INT_B.wait_address("CfgLinkRoute1")

        for i in range(10):
            lr1 = ea1_mgmt.create(type=self.CFG_LINK_ROUTE_TYPE,
                                  name="TestLRout%d" % i,
                                  attributes={'pattern': 'Test/*/%d/#' % i,
                                              'containerId': 'FakeBroker',
                                              'direction': 'out'})
            lr2 = ea1_mgmt.create(type=self.CFG_LINK_ROUTE_TYPE,
                                  name="TestLRin%d" % i,
                                  attributes={'pattern': 'Test/*/%d/#' % i,
                                              'containerId': 'FakeBroker',
                                              'direction': 'in'})
            # verify that they are correctly propagated (once)
            if i == 9:
                self.INT_B.wait_address("Test/*/9/#")
            lr1.delete()
            lr2.delete()


class Timeout(object):
    def __init__(self, parent):
        self.parent = parent

    def on_timer_task(self, event):
        self.parent.timeout()


class PollTimeout(object):
    def __init__(self, parent):
        self.parent = parent

    def on_timer_task(self, event):
        self.parent.poll_timeout()


class ConnectivityTest(MessagingHandler):
    def __init__(self, interior_host, edge_host, edge_id):
        super(ConnectivityTest, self).__init__()
        self.interior_host = interior_host
        self.edge_host     = edge_host
        self.edge_id       = edge_id

        self.interior_conn = None
        self.edge_conn     = None
        self.error         = None
        self.proxy         = None
        self.query_sent    = False

    def timeout(self):
        self.error = "Timeout Expired"
        self.interior_conn.close()
        self.edge_conn.close()

    def on_start(self, event):
        self.timer          = event.reactor.schedule(10.0, Timeout(self))
        self.interior_conn  = event.container.connect(self.interior_host)
        self.edge_conn      = event.container.connect(self.edge_host)
        self.reply_receiver = event.container.create_receiver(self.interior_conn, dynamic=True)

    def on_link_opened(self, event):
        if event.receiver == self.reply_receiver:
            self.proxy        = MgmtMsgProxy(self.reply_receiver.remote_source.address)
            self.agent_sender = event.container.create_sender(self.interior_conn, "$management")

    def on_sendable(self, event):
        if not self.query_sent:
            self.query_sent = True
            self.agent_sender.send(self.proxy.query_connections())

    def on_message(self, event):
        if event.receiver == self.reply_receiver:
            response = self.proxy.response(event.message)
            if response.status_code != 200:
                self.error = "Unexpected error code from agent: %d - %s" % (response.status_code, response.status_description)
            connections = response.results
            count = 0
            for conn in connections:
                if conn.role == 'edge' and conn.container == self.edge_id:
                    count += 1
            if count != 1:
                self.error = "Incorrect edge count for container-id.  Expected 1, got %d" % count
            self.interior_conn.close()
            self.edge_conn.close()
            self.timer.cancel()

    def run(self):
        Container(self).run()


class DynamicAddressTest(MessagingHandler):
    def __init__(self, receiver_host, sender_host):
        super(DynamicAddressTest, self).__init__()
        self.receiver_host = receiver_host
        self.sender_host   = sender_host

        self.receiver_conn = None
        self.sender_conn   = None
        self.receiver      = None
        self.address       = None
        self.count         = 300
        self.n_rcvd        = 0
        self.n_sent        = 0
        self.error         = None

    def timeout(self):
        self.error = "Timeout Expired - n_sent=%d n_rcvd=%d addr=%s" % (self.n_sent, self.n_rcvd, self.address)
        self.receiver_conn.close()
        self.sender_conn.close()

    def on_start(self, event):
        self.timer         = event.reactor.schedule(5.0, Timeout(self))
        self.receiver_conn = event.container.connect(self.receiver_host)
        self.sender_conn   = event.container.connect(self.sender_host)
        self.receiver      = event.container.create_receiver(self.receiver_conn, dynamic=True)

    def on_link_opened(self, event):
        if event.receiver == self.receiver:
            self.address = self.receiver.remote_source.address
            self.sender  = event.container.create_sender(self.sender_conn, self.address)

    def on_sendable(self, event):
        while self.n_sent < self.count:
            self.sender.send(Message(body="Message %d" % self.n_sent))
            self.n_sent += 1

    def on_message(self, event):
        self.n_rcvd += 1
        if self.n_rcvd == self.count:
            self.receiver_conn.close()
            self.sender_conn.close()
            self.timer.cancel()

    def run(self):
        Container(self).run()


class MobileAddressTest(MessagingHandler):
    def __init__(self, receiver_host, sender_host, address):
        super(MobileAddressTest, self).__init__()
        self.receiver_host = receiver_host
        self.sender_host   = sender_host
        self.address       = address

        self.receiver_conn = None
        self.sender_conn   = None
        self.receiver      = None
        self.count         = 300
        self.rel_count     = 50
        self.n_rcvd        = 0
        self.n_sent        = 0
        self.n_settled     = 0
        self.n_released    = 0
        self.error         = None

    def timeout(self):
        self.error = "Timeout Expired - n_sent=%d n_rcvd=%d n_settled=%d n_released=%d addr=%s" % \
                     (self.n_sent, self.n_rcvd, self.n_settled, self.n_released, self.address)
        self.receiver_conn.close()
        self.sender_conn.close()

    def on_start(self, event):
        self.timer         = event.reactor.schedule(5.0, Timeout(self))
        self.receiver_conn = event.container.connect(self.receiver_host)
        self.sender_conn   = event.container.connect(self.sender_host)
        self.receiver      = event.container.create_receiver(self.receiver_conn, self.address)
        self.sender        = event.container.create_sender(self.sender_conn, self.address)

    def on_sendable(self, event):
        while self.n_sent < self.count:
            self.sender.send(Message(body="Message %d" % self.n_sent))
            self.n_sent += 1

    def on_message(self, event):
        self.n_rcvd += 1

    def on_settled(self, event):
        self.n_settled += 1
        if self.n_settled == self.count:
            self.receiver.close()
            for i in range(self.rel_count):
                self.sender.send(Message(body="Message %d" % self.n_sent))
                self.n_sent += 1

    def on_released(self, event):
        self.n_released += 1
        if self.n_released == self.rel_count:
            self.receiver_conn.close()
            self.sender_conn.close()
            self.timer.cancel()

    def run(self):
        Container(self).run()


if __name__ == '__main__':
    unittest.main(main_module())
