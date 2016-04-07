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

import unittest
from time import sleep
from subprocess import PIPE, STDOUT

from system_test import TestCase, Qdrouterd, main_module, TIMEOUT, Process

from proton import Message
from proton.reactor import AtMostOnce
from proton.utils import BlockingConnection, LinkDetached

from qpid_dispatch.management.client import Node

class LinkRoutePatternTest(TestCase):
    """
    Tests the linkRoutePattern property of the dispatch router.

    Sets up 3 routers (one of which is acting as a broker(QDR.A)). 2 routers have linkRoutePattern set to 'org.apache.'
    (please see configs in the setUpClass method to get a sense of how the routers and their connections are configured)
    The tests in this class send and receive messages across this network of routers to link routable addresses.
    Uses the Python Blocking API to send/receive messages. The blocking api plays neatly into the synchronous nature
    of system tests.

        QDR.A acting broker
             +---------+         +---------+         +---------+     +-----------------+
             |         | <------ |         | <-----  |         |<----| blocking_sender |
             |  QDR.A  |         |  QDR.B  |         |  QDR.C  |     +-----------------+
             |         | ------> |         | ------> |         |     +-------------------+
             +---------+         +---------+         +---------+---->| blocking_receiver |
                                                                     +-------------------+
    """
    @classmethod
    def get_router(cls, index):
        return cls.routers[index]

    @classmethod
    def setUpClass(cls):
        """Start three routers"""
        super(LinkRoutePatternTest, cls).setUpClass()

        def router(name, connection):

            config = [
                ('container', {'workerThreads': 4, 'containerName': 'Qpid.Dispatch.Router.%s'%name}),
                ('router', {'mode': 'interior', 'routerId': 'QDR.%s'%name}),
                ('fixedAddress', {'prefix': '/closest/', 'fanout': 'single', 'bias': 'closest'}),
                ('fixedAddress', {'prefix': '/spread/', 'fanout': 'single', 'bias': 'spread'}),
                ('fixedAddress', {'prefix': '/multicast/', 'fanout': 'multiple'}),
                ('fixedAddress', {'prefix': '/', 'fanout': 'multiple'}),

            ] + connection

            config = Qdrouterd.Config(config)
            cls.routers.append(cls.tester.qdrouterd(name, config, wait=False))

        cls.routers = []
        a_listener_port = cls.tester.get_port()
        b_listener_port = cls.tester.get_port()
        c_listener_port = cls.tester.get_port()

        router('A',
               [
                   ('listener', {'role': 'normal', 'addr': '0.0.0.0', 'port': a_listener_port, 'saslMechanisms': 'ANONYMOUS'}),
               ])
        router('B',
               [
                   ('listener', {'role': 'normal', 'addr': '0.0.0.0', 'port': b_listener_port, 'saslMechanisms': 'ANONYMOUS'}),
                   # This is an on-demand connection made from QDR.B's ephemeral port to a_listener_port
                   ('connector', {'name': 'broker', 'role': 'on-demand', 'addr': '0.0.0.0', 'port': a_listener_port, 'saslMechanisms': 'ANONYMOUS'}),
                   # Only inter router communication must happen on 'inter-router' connectors. This connector makes
                   # a connection from the router B's ephemeral port to c_listener_port
                   ('connector', {'role': 'inter-router', 'addr': '0.0.0.0', 'port': c_listener_port}),
                   ('linkRoutePattern', {'prefix': 'org.apache', 'connector': 'broker'})
                ]
               )
        router('C',
               [
                   ('listener', {'addr': '0.0.0.0', 'role': 'inter-router', 'port': c_listener_port, 'saslMechanisms': 'ANONYMOUS'}),
                   # The client will exclusively use the following listener to connect to QDR.C
                   ('listener', {'addr': '0.0.0.0', 'role': 'normal', 'port': cls.tester.get_port(), 'saslMechanisms': 'ANONYMOUS'}),
                   # Note here that the linkRoutePattern is set to org.apache. which makes it backward compatible.
                   # The dot(.) at the end is ignored by the address hashing scheme.
                   ('linkRoutePattern', {'prefix': 'org.apache.'})
                ]
               )

        # Wait for the routers to locate each other
        cls.routers[1].wait_router_connected('QDR.C')
        cls.routers[2].wait_router_connected('QDR.B')

        # This is not a classic router network in the sense that one router is acting as a broker. We allow a little
        # bit more time for the routers to stabilize.
        sleep(2)

    def run_qdstat_linkRoute(self, address):
        p = self.popen(
            ['qdstat', '--bus', str(address), '--timeout', str(TIMEOUT) ] + ['--linkroute'],
            name='qdstat-'+self.id(), stdout=PIPE, expect=None)

        out = p.communicate()[0]
        assert p.returncode == 0, "qdstat exit status %s, output:\n%s" % (p.returncode, out)
        return out

    def run_qdmanage(self, cmd, input=None, expect=Process.EXIT_OK, address=None):
        p = self.popen(
            ['qdmanage'] + cmd.split(' ') + ['--bus', address or self.address(), '--indent=-1', '--timeout', str(TIMEOUT)],
            stdin=PIPE, stdout=PIPE, stderr=STDOUT, expect=expect)
        out = p.communicate(input)[0]
        try:
            p.teardown()
        except Exception, e:
            raise Exception("%s\n%s" % (e, out))
        return out

    def test_aaa_qdmanage_query_link_route(self):
        """
        qdmanage converts short type to long type and this test specifically tests if qdmanage is actually doing
        the type conversion correctly by querying with short type and long type.
        """
        cmd = 'QUERY --type=linkRoute'
        out = self.run_qdmanage(cmd=cmd, address=self.routers[1].addresses[0])

        # Make sure there is a dir of in and out.
        self.assertTrue('"dir": "in"' in out)
        self.assertTrue('"dir": "out"' in out)
        self.assertTrue('"connection": "broker"' in out)

        # Use the long type and make sure that qdmanage does not mess up the long type
        cmd = 'QUERY --type=org.apache.qpid.dispatch.router.config.linkRoute'
        out = self.run_qdmanage(cmd=cmd, address=self.routers[1].addresses[0])

        # Make sure there is a dir of in and out.
        self.assertTrue('"dir": "in"' in out)
        self.assertTrue('"dir": "out"' in out)
        self.assertTrue('"connection": "broker"' in out)

    def test_bbb_qdstat_link_routes_routerB(self):
        """
        Runs qdstat on router B to make sure that router B has two link routes, one 'in' and one 'out'

        """
        out = self.run_qdstat_linkRoute(self.routers[1].addresses[0])
        out_list = out.split()
        self.assertEqual(out_list.count('in'), 1)
        self.assertEqual(out_list.count('out'), 1)

    def test_ccc_qdstat_link_routes_routerC(self):
        """
        Runs qdstat on router C to make sure that router C has two link routes, one 'in' and one 'out'

        """
        out = self.run_qdstat_linkRoute(self.routers[2].addresses[1])
        out_list = out.split()

        self.assertEqual(out_list.count('in'), 1)
        self.assertEqual(out_list.count('out'), 1)

    def test_ddd_partial_link_route_match(self):
        """
        The linkRoutePattern on Routers C and B is set to org.apache.
        Creates a receiver listening on the address 'org.apache.dev' and a sender that sends to address 'org.apache.dev'.
        Sends a message to org.apache.dev via router QDR.C and makes sure that the message was successfully
        routed (using partial address matching) and received using pre-created links that were created as a
        result of specifying addresses in the linkRoutePattern attribute('org.apache.').
        """
        hello_world_1 = "Hello World_1!"

        # Connects to listener #2 on QDR.C
        addr = self.routers[2].addresses[1]

        blocking_connection = BlockingConnection(addr)

        # Receive on org.apache.dev
        blocking_receiver = blocking_connection.create_receiver(address="org.apache.dev")

        apply_options = AtMostOnce()

        # Sender to org.apache.dev
        blocking_sender = blocking_connection.create_sender(address="org.apache.dev", options=apply_options)
        msg = Message(body=hello_world_1)
        # Send a message
        blocking_sender.send(msg)

        received_message = blocking_receiver.receive()

        self.assertEqual(hello_world_1, received_message.body)

        # Connect to the router acting like the broker (QDR.A) and check the deliveriesIngress and deliveriesEgress
        local_node = Node.connect(self.routers[0].addresses[0], timeout=TIMEOUT)
        self.assertEqual(u'QDR.A', local_node.query(type='org.apache.qpid.dispatch.router',
                                                    attribute_names=['routerId']).results[0][0])

        self.assertEqual(1, local_node.read(type='org.apache.qpid.dispatch.router.address',
                                            name='M0org.apache.dev').deliveriesEgress,
                         "deliveriesEgress is wrong")
        self.assertEqual(1, local_node.read(type='org.apache.qpid.dispatch.router.address',
                                            name='M0org.apache.dev').deliveriesIngress,
                         "deliveriesIngress is wrong")

        # There should be 4 links -
        # 1. outbound receiver link on org.apache.dev
        # 2. inbound sender link on blocking_sender
        # 3. inbound link to the $management
        # 4. outbound link to $management
        # self.assertEqual(4, len()
        self.assertEquals(4, len(local_node.query(type='org.apache.qpid.dispatch.router.link').results))

        #blocking_receiver.close()
        blocking_connection.close()

    def test_partial_link_route_match_1(self):
        """
        This test is pretty much the same as the previous test (test_partial_link_route_match) but the connection is
        made to router QDR.B instead of QDR.C and we expect to see the same behavior.
        """
        hello_world_2 = "Hello World_2!"
        addr = self.routers[1].addresses[0]

        blocking_connection = BlockingConnection(addr)

        # Receive on org.apache.dev
        blocking_receiver = blocking_connection.create_receiver(address="org.apache.dev")

        apply_options = AtMostOnce()

        # Sender to  to org.apache.dev
        blocking_sender = blocking_connection.create_sender(address="org.apache.dev", options=apply_options)
        msg = Message(body=hello_world_2)
        # Send a message
        blocking_sender.send(msg)

        received_message = blocking_receiver.receive()

        self.assertEqual(hello_world_2, received_message.body)

        local_node = Node.connect(self.routers[0].addresses[0], timeout=TIMEOUT)

        # Make sure that the router node acting as the broker (QDR.A) had one message routed through it. This confirms
        # that the message was link routed
        self.assertEqual(1, local_node.read(type='org.apache.qpid.dispatch.router.address',
                                            name='M0org.apache.dev').deliveriesEgress,
                         "deliveriesEgress is wrong")

        self.assertEqual(1, local_node.read(type='org.apache.qpid.dispatch.router.address',
                                            name='M0org.apache.dev').deliveriesIngress,
                         "deliveriesIngress is wrong")

        #blocking_receiver.close()
        blocking_connection.close()

    def test_full_link_route_match(self):
        """
        The linkRoutePattern on Routers C and B is set to org.apache.
        Creates a receiver listening on the address 'org.apache' and a sender that sends to address 'org.apache'.
        Sends a message to org.apache via router QDR.C and makes sure that the message was successfully
        routed (using full address matching) and received using pre-created links that were created as a
        result of specifying addresses in the linkRoutePattern attribute('org.apache.').
        """
        hello_world_3 = "Hello World_3!"
        # Connects to listener #2 on QDR.C
        addr = self.routers[2].addresses[1]

        blocking_connection = BlockingConnection(addr)

        # Receive on org.apache
        blocking_receiver = blocking_connection.create_receiver(address="org.apache")

        apply_options = AtMostOnce()

        # Sender to  to org.apache
        blocking_sender = blocking_connection.create_sender(address="org.apache", options=apply_options)
        msg = Message(body=hello_world_3)
        # Send a message
        blocking_sender.send(msg)

        received_message = blocking_receiver.receive()

        self.assertEqual(hello_world_3, received_message.body)

        local_node = Node.connect(self.routers[0].addresses[0], timeout=TIMEOUT)

        # Make sure that the router node acting as the broker (QDR.A) had one message routed through it. This confirms
        # that the message was link routed
        self.assertEqual(1, local_node.read(type='org.apache.qpid.dispatch.router.address',
                                            name='M0org.apache').deliveriesEgress,
                         "deliveriesEgress is wrong")

        self.assertEqual(1, local_node.read(type='org.apache.qpid.dispatch.router.address',
                                            name='M0org.apache').deliveriesIngress,
                         "deliveriesIngress is wrong")

        #blocking_receiver.close()
        blocking_connection.close()

    def test_full_link_route_match_1(self):
        """
        This test is pretty much the same as the previous test (test_full_link_route_match) but the connection is
        made to router QDR.B instead of QDR.C and we expect the message to be link routed successfully.
        """
        hello_world_4 = "Hello World_4!"
        addr = self.routers[2].addresses[0]

        blocking_connection = BlockingConnection(addr)

        # Receive on org.apache
        blocking_receiver = blocking_connection.create_receiver(address="org.apache")

        apply_options = AtMostOnce()

        # Sender to  to org.apache
        blocking_sender = blocking_connection.create_sender(address="org.apache", options=apply_options)
        msg = Message(body=hello_world_4)
        # Send a message
        blocking_sender.send(msg)

        received_message = blocking_receiver.receive()

        self.assertEqual(hello_world_4, received_message.body)

        local_node = Node.connect(self.routers[0].addresses[0], timeout=TIMEOUT)

        # Make sure that the router node acting as the broker (QDR.A) had one message routed through it. This confirms
        # that the message was link routed
        self.assertEqual(1, local_node.read(type='org.apache.qpid.dispatch.router.address',
                                            name='M0org.apache').deliveriesEgress,
                         "deliveriesEgress is wrong")

        self.assertEqual(1, local_node.read(type='org.apache.qpid.dispatch.router.address',
                                            name='M0org.apache').deliveriesIngress,
                         "deliveriesIngress is wrong")

        #blocking_receiver.close()
        blocking_connection.close()

    def test_zzz_qdmanage_delete_link_route(self):
        """
        We are deleting the link route using qdmanage short name. This should be the last test to run
        """

        # First delete linkRoutes on QDR.B
        local_node = Node.connect(self.routers[1].addresses[0], timeout=TIMEOUT)
        result_list = local_node.query(type='org.apache.qpid.dispatch.router.config.linkRoute').results

        identity_1 = result_list[0][1]
        identity_2 = result_list[1][1]

        cmd = 'DELETE --type=linkRoute --identity=' + identity_1
        self.run_qdmanage(cmd=cmd, address=self.routers[1].addresses[0])

        cmd = 'DELETE --type=linkRoute --identity=' + identity_2
        self.run_qdmanage(cmd=cmd, address=self.routers[1].addresses[0])

        cmd = 'QUERY --type=linkRoute'
        out = self.run_qdmanage(cmd=cmd, address=self.routers[1].addresses[0])
        self.assertEquals(out.rstrip(), '[]')

        sleep(1)

        # linkRoutes now gone on QDR.B but remember that it still exist on QDR.C
        # We will now try to create a receiver on address org.apache.dev on QDR.C.
        # Since the linkRoute on QDR.B is gone, QDR.C
        # will not allow a receiver to be created since there is no route to destination.
        # Connects to listener #2 on QDR.C
        addr = self.routers[2].addresses[1]

        timeout_exception = False
        blocking_connection = BlockingConnection(addr, timeout=3)

        try:
            blocking_connection.create_receiver(address="org.apache.dev")
        except Exception as e:
            self.assertTrue("timed out: Opening link" in e.message)
            timeout_exception = True

        self.assertTrue(timeout_exception)

        # Now delete linkRoutes on QDR.C to eradicate linkRoutes completely
        local_node = Node.connect(addr, timeout=TIMEOUT)
        result_list = local_node.query(type='org.apache.qpid.dispatch.router.config.linkRoute').results

        identity_1 = result_list[0][1]
        identity_2 = result_list[1][1]

        cmd = 'DELETE --type=linkRoute --identity=' + identity_1
        self.run_qdmanage(cmd=cmd, address=addr)

        cmd = 'DELETE --type=linkRoute --identity=' + identity_2
        self.run_qdmanage(cmd=cmd, address=addr)

        cmd = 'QUERY --type=linkRoute'
        out = self.run_qdmanage(cmd=cmd, address=addr)
        self.assertEquals(out.rstrip(), '[]')

        blocking_connection = BlockingConnection(addr, timeout=3)

        # Receive on org.apache.dev (this address used to be linkRouted but not anymore since we deleted linkRoutes
        # on both QDR.C and QDR.B)
        blocking_receiver = blocking_connection.create_receiver(address="org.apache.dev")

        apply_options = AtMostOnce()
        hello_world_1 = "Hello World_1!"
        # Sender to org.apache.dev
        blocking_sender = blocking_connection.create_sender(address="org.apache.dev", options=apply_options)
        msg = Message(body=hello_world_1)

        # Send a message
        blocking_sender.send(msg)
        received_message = blocking_receiver.receive(timeout=5)
        self.assertEqual(hello_world_1, received_message.body)

        # Connect to the router acting like the broker (QDR.A) and check the deliveriesIngress and deliveriesEgress
        local_node = Node.connect(self.routers[2].addresses[1], timeout=TIMEOUT)

        self.assertEqual(u'QDR.C', local_node.query(type='org.apache.qpid.dispatch.router',
                                                    attribute_names=['routerId']).results[0][0])

        self.assertEqual(1, local_node.read(type='org.apache.qpid.dispatch.router.address',
                                            name='M0org.apache.dev').deliveriesEgress,
                         "deliveriesEgress is wrong")

if __name__ == '__main__':
    unittest.main(main_module())
