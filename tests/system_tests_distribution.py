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

import unittest, os, json
from subprocess      import PIPE, STDOUT
from proton          import Message, PENDING, ACCEPTED, REJECTED, RELEASED, SSLDomain, SSLUnavailable, Timeout
from system_test     import TestCase, Qdrouterd, main_module, DIR, TIMEOUT, Process
from proton.handlers import MessagingHandler
from proton.reactor  import Container, AtMostOnce, AtLeastOnce, DynamicNodeProperties, LinkOption
from proton.utils    import BlockingConnection
from qpid_dispatch.management.client import Node

import time


# PROTON-828:
try:
    from proton import MODIFIED
except ImportError:
    from proton import PN_STATUS_MODIFIED as MODIFIED




#------------------------------------------------
# Helper classes for all tests.
#------------------------------------------------

class Timeout(object):
    def __init__(self, parent):
        self.parent = parent

    def on_timer_task(self, event):
        self.parent.timeout()



class AddressCheckResponse(object):
    """
    Convenience class for the responses returned by an AddressChecker.
    """
    def __init__(self, status_code, status_description, attrs):
        self.status_code        = status_code
        self.status_description = status_description
        self.attrs              = attrs

    def __getattr__(self, key):
        return self.attrs[key]



class AddressChecker ( object ):
    """
    Format address-query messages and parse the responses.
    """
    def __init__ ( self, reply_addr ):
        self.reply_addr = reply_addr

    def parse_address_query_response ( self, msg ):
        ap = msg.properties
        return AddressCheckResponse ( ap['statusCode'], ap['statusDescription'], msg.body )

    def make_address_query ( self, name ):
        ap = {'operation': 'READ', 'type': 'org.apache.qpid.dispatch.router.address', 'name': name}
        return Message ( properties=ap, reply_to=self.reply_addr )

    def make_addresses_query ( self ):
        ap = {'operation': 'QUERY', 'type': 'org.apache.qpid.dispatch.router.address'}
        return Message ( properties=ap, reply_to=self.reply_addr )



class AddressCheckerTimeout ( object ):
    def __init__(self, parent):
        self.parent = parent

    def on_timer_task(self, event):
        self.parent.address_check_timeout()

#------------------------------------------------
# END Helper classes for all tests.
#------------------------------------------------




#================================================================
#     Setup
#================================================================

class DistributionTests ( TestCase ):

    @classmethod
    def setUpClass(cls):
        """
        Create a router topology that is a superset of the topologies we will
        need for various tests.  So far, we have only two types of tests:
        3-router linear, and 3-router triangular.  The various tests simply
        attach their senders and receivers appropriately to 'see' their
        desired topology.
        """
        super(DistributionTests, cls).setUpClass()


        def router(name, more_config):

            config = [ ('router',  {'mode': 'interior', 'id': name}),
                       ('address', {'prefix': 'closest',   'distribution': 'closest'}),
                       ('address', {'prefix': 'balanced',  'distribution': 'balanced'}),
                       ('address', {'prefix': 'multicast', 'distribution': 'multicast'})
                     ] + more_config

            config = Qdrouterd.Config(config)

            cls.routers.append(cls.tester.qdrouterd(name, config, wait=True))

        cls.routers = []



        #
        #     Connection picture
        #
        #           1           1
        #         A <-------- B <------ C
        #          ^ 2       ^ 2
        #           \       /
        #            \     /
        #             \   /
        #              \ /
        #               D
        #
        #

        A_client_port          = cls.tester.get_port()
        B_client_port          = cls.tester.get_port()
        C_client_port          = cls.tester.get_port()
        D_client_port          = cls.tester.get_port()

        A_inter_router_port_1  = cls.tester.get_port()
        A_inter_router_port_2  = cls.tester.get_port()
        B_inter_router_port_1  = cls.tester.get_port()
        B_inter_router_port_2  = cls.tester.get_port()

        # "Route-container port" does not mean that the port
        # contains a route.  It means that any client that
        # connectsd to the port is considered to be a route-
        # container.
        A_route_container_port = cls.tester.get_port()
        B_route_container_port = cls.tester.get_port()
        C_route_container_port = cls.tester.get_port()
        D_route_container_port = cls.tester.get_port()

        # Costs for balanced tests. The 'balanced' distribution
        # takes these costs into account in its algorithm.
        # Costs are associated not with routers, but with the
        # connections between routers.  In the config, they may
        # be attached to the inter-router listener, or the connector,
        # or both.  If both the inter-router listener and the
        # connector have associated costs, the higher of the two
        # will be used.
        cls.A_B_cost =   10
        cls.B_C_cost =   20
        cls.A_D_cost =   50
        cls.B_D_cost =  100

        cls.linkroute_prefix = "0.0.0.0/linkroute"

        router ( 'A',
                 [
                    ( 'listener',
                      { 'port': A_client_port,
                        'role': 'normal',
                        'stripAnnotations': 'no'
                      }
                    ),
                    ( 'listener',
                      {  'role': 'inter-router',
                         'port': A_inter_router_port_1
                      }
                    ),
                    ( 'listener',
                      {  'role': 'inter-router',
                         'port': A_inter_router_port_2
                      }
                    ),
                    ( 'listener',
                      { 'port': A_route_container_port,
                        'stripAnnotations': 'no',
                        'role': 'route-container'
                      }
                    ),
                    ( 'linkRoute',
                      { 'prefix': cls.linkroute_prefix,
                        'dir': 'in',
                        'containerId': 'LinkRouteTest'
                      }
                    ),
                    ( 'linkRoute',
                      { 'prefix': cls.linkroute_prefix,
                        'dir': 'out',
                        'containerId': 'LinkRouteTest'
                      }
                    )
                 ]
               )

        router ( 'B',
                 [
                    ( 'listener',
                      { 'port': B_client_port,
                        'role': 'normal',
                        'stripAnnotations': 'no'
                      }
                    ),
                    ( 'listener',
                      {  'role': 'inter-router',
                         'port': B_inter_router_port_1
                      }
                    ),
                    ( 'listener',
                      {  'role': 'inter-router',
                         'port': B_inter_router_port_2
                      }
                    ),
                    ( 'listener',
                      { 'port': B_route_container_port,
                        'stripAnnotations': 'no',
                        'role': 'route-container'
                      }
                    ),
                    ( 'linkRoute',
                      { 'prefix': cls.linkroute_prefix,
                        'dir': 'in',
                        'containerId': 'LinkRouteTest'
                      }
                    ),
                    ( 'linkRoute',
                      { 'prefix': cls.linkroute_prefix,
                        'dir': 'out',
                        'containerId': 'LinkRouteTest'
                      }
                    ),
                    ( 'connector',
                      {  'name': 'connectorToA',
                         'role': 'inter-router',
                         'port': A_inter_router_port_1,
                         'verifyHostName': 'no',
                         'cost':  cls.A_B_cost
                      }
                    )
                 ]
               )

        router ( 'C',
                 [
                    ( 'listener',
                      { 'port': C_client_port,
                        'role': 'normal',
                        'stripAnnotations': 'no'
                      }
                    ),
                    ( 'listener',
                       { 'port': C_route_container_port,
                         'stripAnnotations': 'no',
                         'role': 'route-container'
                       }
                    ),
                    ( 'linkRoute',
                      { 'prefix': cls.linkroute_prefix,
                        'dir': 'in',
                        'containerId': 'LinkRouteTest'
                      }
                    ),
                    ( 'linkRoute',
                      { 'prefix': cls.linkroute_prefix,
                        'dir': 'out',
                        'containerId': 'LinkRouteTest'
                      }
                    ),
                    ( 'connector',
                      {  'name': 'connectorToB',
                         'role': 'inter-router',
                         'port': B_inter_router_port_1,
                         'verifyHostName': 'no',
                         'cost' : cls.B_C_cost
                      }
                    )
                 ]
               )

        router ( 'D',
                 [
                    ( 'listener',
                      { 'port': D_client_port,
                        'role': 'normal',
                        'stripAnnotations': 'no'
                      }
                    ),
                    ( 'listener',
                       { 'port': D_route_container_port,
                         'stripAnnotations': 'no',
                         'role': 'route-container'
                       }
                    ),
                    ( 'connector',
                      {  'name': 'connectorToA',
                         'role': 'inter-router',
                         'port': A_inter_router_port_2,
                         'verifyHostName': 'no',
                         'cost' : cls.A_D_cost
                      }
                    ),
                    ( 'linkRoute',
                      { 'prefix': cls.linkroute_prefix,
                        'dir': 'in',
                        'containerId': 'LinkRouteTest'
                      }
                    ),
                    ( 'linkRoute',
                      { 'prefix': cls.linkroute_prefix,
                        'dir': 'out',
                        'containerId': 'LinkRouteTest'
                      }
                    ),
                    ( 'connector',
                      {  'name': 'connectorToB',
                         'role': 'inter-router',
                         'port': B_inter_router_port_2,
                         'verifyHostName': 'no',
                         'cost' : cls.B_D_cost
                      }
                    )
                 ]
               )

        router_A = cls.routers[0]
        router_B = cls.routers[1]
        router_C = cls.routers[2]
        router_D = cls.routers[3]

        cls.A_route_container_addr = router_A.addresses[3]
        cls.B_route_container_addr = router_B.addresses[3]
        cls.C_route_container_addr = router_B.addresses[1]
        cls.D_route_container_addr = router_B.addresses[1]

        router_A.wait_router_connected('B')
        router_A.wait_router_connected('C')
        router_A.wait_router_connected('D')

        cls.A_addr = router_A.addresses[0]
        cls.B_addr = router_B.addresses[0]
        cls.C_addr = router_C.addresses[0]
        cls.D_addr = router_D.addresses[0]


 
    def test_01_targeted_sender_AC ( self ):
        test = TargetedSenderTest ( self.A_addr, self.C_addr, "closest/01" )
        test.run()
        self.assertEqual ( None, test.error )
 
 
    def test_02_targeted_sender_DC ( self ):
        test = TargetedSenderTest ( self.D_addr, self.C_addr, "closest/02" )
        test.run()
        self.assertEqual ( None, test.error )
 
 
    def test_03_anonymous_sender_AC ( self ):
        test = AnonymousSenderTest ( self.A_addr, self.C_addr )
        test.run()
        self.assertEqual ( None, test.error )
 
 
    def test_04_anonymous_sender_DC ( self ):
        test = AnonymousSenderTest ( self.D_addr, self.C_addr )
        test.run()
        self.assertEqual ( None, test.error )
 
 
    def test_05_dynamic_reply_to_AC ( self ):
        test = DynamicReplyTo ( self.A_addr, self.C_addr )
        test.run()
        self.assertEqual ( None, test.error )
 
 
    def test_06_dynamic_reply_to_DC ( self ):
        test = DynamicReplyTo ( self.D_addr, self.C_addr )
        test.run()
        self.assertEqual ( None, test.error )
 
 
    def test_07_linkroute ( self ):
        test = LinkAttachRouting ( self.C_addr,
                                   self.A_route_container_addr,
                                   self.linkroute_prefix,
                                   "addr_07"
                                 )
        test.run()
        self.assertEqual ( None, test.error )
 
 
    def test_08_closest_linear ( self ):
        test = ClosestTest ( self.A_addr,
                             self.B_addr,
                             self.C_addr,
                             "addr_08"
                           )
        test.run()
        self.assertEqual ( None, test.error )
 
 
    def test_09_closest_mesh ( self ):
        test = ClosestTest ( self.A_addr,
                             self.B_addr,
                             self.D_addr,
                             "addr_09"
                           )
        test.run()
        self.assertEqual ( None, test.error )
 
        #
        #     Cost picture for balanced distribution tests.
        #
        #              10          20
        #         A <-------- B <------ C
        #          ^         ^
        #           \       /
        #       50   \     /  100
        #             \   /
        #              \ /
        #               D
        #
        #
        #
        #  Here is how the message balancing should work for
        #  various total number of messages, up to 100:
        #
        #  NOTE: remember these messages are all unsettled.
        #        And will stay that way.  This is not a realistic
        #        usage scenario, but it the best way to test the
        #        balanced distribution algorithm.
        #
        #  1. Messages start flowing in at A.  They will all
        #     be used by A (sent to its receiver) until the
        #     total == cost ( A, B ).
        #
        #  2. At that point, A will start sharing with B,
        #     one-for-me-one-for-you. (So A will go to 11 before
        #     B gets its first message.)
        #
        #  3. A and B will count up until B reaches
        #     cost ( B, C )
        #     B will then start sharings its messages with C,
        #     one-for-me-one-for-you.  (So B will go to 21 before
        #     C gets its first message.)
        #
        #  4. However note: it is NOT round-robin at this point.
        #     A is still taking every other message, B is only getting
        #     A's overflow, and now B is sharing half of that with C.
        #     So at this point B will start falling farther behind A.
        #
        #  5. The totals here are completely deterministic, so we pass
        #     to the test a 'slop' amount of 0.
        #
        #    total   near --10--> mid ---20--> far
        #
        #     1        1            0            0
        #     10      10            0            0
        #     11      11            0            0
        #     12      11            1            0
        #     13      12            1            0
        #     14      12            2            0
        #     ...
        #     50      30           20            0
        #     51      31           20            0
        #     52      31           21            0
        #     53      32           21            0
        #     54      32           21            1
        #     55      33           21            1
        #     56      33           22            1
        #     57      34           22            1
        #     58      34           22            2
        #     59      35           22            2
        #     60      35           23            2
        #     ...
        #     100     55           33           12
        #
 
    def test_10_balanced_linear ( self ):
        # slop is how much the second two values may diverge from
        # the expected.  But they still must sum to total - A.
        total      = 100
        expected_A = 55
        expected_B = 33
        expected_C = 12
        slop       = 0
        omit_middle_receiver = False
 
        test = BalancedTest ( self.A_addr,
                              self.B_addr,
                              self.C_addr,
                              "addr_10",
                              total,
                              expected_A,
                              expected_B,
                              expected_C,
                              slop,
                              omit_middle_receiver
                            )
        test.run()
        self.assertEqual ( None, test.error )
 
 
    def test_11_balanced_linear_omit_middle_receiver ( self ):
        # If we omit the middle receiver, then router A will count
        # up to cost ( A, B ) and the keep counting up a further
        # cost ( B, C ) before it starts to spill over.
        # That is, it will count up to
        #    cost ( A, B ) + cost ( B, C ) == 30
        # After that it will start sharing downstream (router C)
        # one-for-me-one-for-you.  So when the number of total messages
        # is odd, A will be 31 ahead of C.  When total message count is
        # even, A will be 30 ahead.
        # As in the other linear scenario, there is no 'slop' here.
        total      = 100
        expected_A = 65
        expected_B = 0
        expected_C = 35
        slop       = 0
        omit_middle_receiver = True
 
        test = BalancedTest ( self.A_addr,
                              self.B_addr,
                              self.C_addr,
                              "addr_11",
                              total,
                              expected_A,
                              expected_B,
                              expected_C,
                              slop,
                              omit_middle_receiver
                            )
        test.run()
        self.assertEqual ( None, test.error )
 
 
        #     Reasoning for the triangular balanced case:
        #
        #     Cost picture
        #
        #              10          20
        #         A <-------- B <------ C
        #          ^         ^
        #           \       /
        #       50   \     /  100
        #             \   /
        #              \ /
        #               D
        #
        # We are doing  ( A, B, D ), with the sender attached at A.
        # All these messages are unsettled, which is what allows us to
        # see how the balanced distribution algorithm works.
        #
        #  1. total unsettled msgs at A cannot be more than B_cost + 1,
        #     and also cannot be more than D_cost + 1
        #
        #  2. A will always keep the message for itself (for its own receiver)
        #     if it can do so without violating rule (1).
        #
        #  3. So, A will count up to 11, and then it will start alternating
        #     with B.
        #
        #  4. When A counts up to 51, it must also start sharing with D.
        #     It will alternate between B and D.
        #
        #  5. As long as B does not yet have 100 messages, it will not
        #     share with D.
        #
        #  6. So! at 100 messages total, A must be above both of its
        #     neighbors by that neighbor's cost, or 1 more -- and the total
        #     of all 3 must sum to 100.
        #
        #     A = B + 10      B = A - 10
        #     A = D + 50      D = A - 50
        #     A + B + D == 100
        #     -->
        #     A + (A - 10) + (A - 50) == 100
        #     3A - 60 == 100
        #     A == 53.333...
        #     A == 54
        #
        #     so B + D == 46
        #     A is 10 or 11 > B --> B == 44 or 43
        #     A is 50 or 51 > D --> D ==  4 or  3
        #     B == 43 and D == 3
        #
        #     So pass these values in to the test: (54, 43, 3)
        #     and test that:
        #       1. A is exactly that value.
        #       2. B and D sum to 100 - A
        #       3. B and D are both with 1 of their expected values.
        #
    def test_12_balanced_mesh ( self ):
        total      = 100
        expected_A = 54
        expected_B = 43
        expected_D = 3
        slop       = 1
        omit_middle_receiver = False
        test = BalancedTest ( self.A_addr,
                              self.B_addr,
                              self.D_addr,
                              "addr_12",
                              total,
                              expected_A,
                              expected_B,
                              expected_D,
                              slop,
                              omit_middle_receiver
                            )
        test.run()
        self.assertEqual ( None, test.error )
 
 
    def test_13_multicast_linear ( self ):
        test = MulticastTest ( self.A_addr,
                               self.B_addr,
                               self.C_addr,
                               "addr_13"
                             )
        test.run()
        self.assertEqual ( None, test.error )
 
 
    def test_14_multicast_mesh ( self ):
        test = MulticastTest ( self.A_addr,
                               self.B_addr,
                               self.D_addr,
                               "addr_14"
                             )
        test.run()
        self.assertEqual ( None, test.error )
 
 
    def test_15_linkroute_linear_all_local ( self ) :
        """
        This test should route all senders' link-attaches
        to the local containers on router A.
        """

        addr_suffix = "addr_15"

        # Choose which routers to give the test.
        # This choice controls topology.  ABC is linear.
        routers = ( self.A_route_container_addr,
                    self.B_route_container_addr,
                    self.C_route_container_addr
                  )

        # Tell the test on which routers to make its link-container cnxs.
        where_to_make_connections                = ( 2, 2, 2 )
        where_the_routed_link_attaches_should_go = ( 4, 0, 0 )

        #-----------------------------------------------------------------------
        # This is the instruction-list that the test looks at as various
        # milestones are met during testing. If a given event happens,
        # and if it matches the event in the current step of the instructions,
        # then the test will execute the action in the current step, and
        # advance to the next.
        # These instructions lists make the test more flexible, so I can get
        # different behavior without writing *almost* the same code mutiple
        # times.
        #-----------------------------------------------------------------------

        # note: if 'done' is present in an action, it always means 'succeed now'.
        # If there had been a failure, that would have been caught in an
        # earlier part of ther action.

        instructions = [
                         # Once the link-routable address is ready to use in
                         # the router network, create 4 senders.
                         {
                           'event'  : 'address_ready',
                           'action' : { 'fn'   : 'make_senders',
                                         'arg' : 4
                                      }
                         },
                         # In this action, the list-argument to the function
                         # shows how we expect link-attach routes to be
                         # distributed: 4 to the first router,
                         # none to the other two.
                         {
                           'event'  : 'got_receivers',
                           'action' : { 'fn'   : 'check_receiver_distribution',
                                        'arg'  : where_the_routed_link_attaches_should_go,
                                      }
                         },
                         {
                           'event'  : 'receiver_distribution_ok',
                           'action' : {'fn'    : 'none',
                                       'done'  : 'succeed'
                                      }
                         }
                       ]

        # Tell the test how to check for the address being ready.
        n_local_containers = 2
        n_remote_routers   = 1  # NOTE: BUGALERT -- THIS SHOULD BE 2

        test = RoutingTest ( self.A_addr,  # all senders are attached here
                             routers,
                             self.linkroute_prefix,
                             addr_suffix,
                             instructions,
                             where_to_make_connections,
                             n_local_containers,
                             n_remote_routers,
                             "Test 15"
                           )
        test.run ( )
        self.assertEqual ( None, test.error )



    def test_16_linkroute_linear_all_B ( self ) :
        """
        This test should route all senders' link-attaches
        to the remote connections on router B.
        """

        addr_suffix = "addr_16"

        # Choose which routers to give the test.
        # This choice controls topology.  ABC is linear.
        routers = ( self.A_route_container_addr,
                    self.B_route_container_addr,
                    self.C_route_container_addr
                  )

        # Tell the test on which routers to make its link-container cnxs.
        where_to_make_connections                = ( 0, 2, 2 )
        where_the_routed_link_attaches_should_go = ( 0, 4, 0 )

        #-----------------------------------------------------------------------
        # This is the instruction-list that the test looks at as various
        # milestones are met during testing. If a given event happens,
        # and if it matches the event in the current step of the instructions,
        # then the test will execute the action in the current step, and
        # advance to the next.
        # These instructions lists make the test more flexible, so I can get
        # different behavior without writing *almost* the same code mutiple
        # times.
        #-----------------------------------------------------------------------

        # note: if 'done' is present in an action, it always means 'succeed now'.
        # If there had been a failure, that would have been caught in an
        # earlier part of ther action.

        instructions = [
                         # Once the link-routable address is ready to use in
                         # the router network, create 4 senders.
                         {
                           'event'  : 'address_ready',
                           'action' : { 'fn'   : 'make_senders',
                                         'arg' : 4
                                      }
                         },
                         # In this action, the list-argument to the function
                         # shows how we expect link-attach routes to be
                         # distributed: 4 to router B,
                         # none anywhere else.
                         {
                           'event'  : 'got_receivers',
                           'action' : { 'fn'   : 'check_receiver_distribution',
                                        'arg'  : where_the_routed_link_attaches_should_go,
                                      }
                         },
                         {
                           'event'  : 'receiver_distribution_ok',
                           'action' : {'fn'    : 'none',
                                       'done'  : 'succeed'
                                      }
                         }
                       ]

        # Tell the test how to check for the address being ready.
        n_local_containers = 0
        n_remote_routers   = 1  # NOTE: BUGALERT -- THIS SHOULD BE 2

        test = RoutingTest ( self.A_addr,  # all senders are attached here
                             routers,
                             self.linkroute_prefix,
                             addr_suffix,
                             instructions,
                             where_to_make_connections,
                             n_local_containers,
                             n_remote_routers,
                             "Test 16"
                           )
        test.run ( )
        self.assertEqual ( None, test.error )



    def test_17_linkroute_linear_all_C ( self ) :
        """
        This test should route all senders' link-attaches
        to the remote connections on router C.
        """

        self.skipTest("This test disabled pending bugfix.")

        addr_suffix = "addr_17"

        # Choose which routers to give the test.
        # This choice controls topology.  ABC is linear.
        routers = ( self.A_route_container_addr,
                    self.B_route_container_addr,
                    self.C_route_container_addr
                  )

        # Tell the test on which routers to make its link-container cnxs.
        where_to_make_connections                = ( 0, 0, 2 )
        where_the_routed_link_attaches_should_go = ( 0, 0, 4 )

        #-----------------------------------------------------------------------
        # This is the instruction-list that the test looks at as various
        # milestones are met during testing. If a given event happens,
        # and if it matches the event in the current step of the instructions,
        # then the test will execute the action in the current step, and
        # advance to the next.
        # These instructions lists make the test more flexible, so I can get
        # different behavior without writing *almost* the same code mutiple
        # times.
        #-----------------------------------------------------------------------

        # note: if 'done' is present in an action, it always means 'succeed now'.
        # If there had been a failure, that would have been caught in an
        # earlier part of ther action.

        instructions = [
                         # Once the link-routable address is ready to use in
                         # the router network, create 4 senders.
                         {
                           'event'  : 'address_ready',
                           'action' : { 'fn'   : 'make_senders',
                                         'arg' : 4
                                      }
                         },
                         # In this action, the list-argument to the function
                         # shows how we expect link-attach routes to be
                         # distributed: 4 to router B,
                         # none anywhere else.
                         {
                           'event'  : 'got_receivers',
                           'action' : { 'fn'   : 'check_receiver_distribution',
                                        'arg'  : where_the_routed_link_attaches_should_go
                                      }
                         },
                         {
                           'event'  : 'receiver_distribution_ok',
                           'action' : {'fn'    : 'none',
                                       'done'  : 'succeed'
                                      }
                         }
                       ]

        # Tell the test how to check for the address being ready.
        n_local_containers = 0
        n_remote_routers   = 1  # NOTE: BUGALERT -- THIS SHOULD BE 2

        test = RoutingTest ( self.A_addr,  # all senders are attached here
                             routers,
                             self.linkroute_prefix,
                             addr_suffix,
                             instructions,
                             where_to_make_connections,
                             n_local_containers,
                             n_remote_routers,
                             "Test 17"
                           )
        test.run ( )
        self.assertEqual ( None, test.error )


    def test_18_linkroute_linear_kill_1 ( self ) :
        """
        Start out as usual, making four senders and seeing their link-attaches
        routed to router A (local). But then kill the two route-container
        connections to router A, and make four more senders.  Their link-attaches
        should get routed to router B.
        """

        self.skipTest("This test disabled pending bugfix.")

        addr_suffix = "addr_18"

        # Choose which routers to give the test.
        # This choice controls topology.  ABC is linear.
        routers = ( self.A_route_container_addr,
                    self.B_route_container_addr,
                    self.C_route_container_addr
                  )

        # Tell the test on which routers to make its link-container cnxs.
        where_to_make_connections = ( 2, 2, 2 )

        # And where to expect the resulting link-attaches to end up.
        first_4                   = ( 4, 0, 0 )   # All go to A
        second_4                  = ( 0, 4, 0 )   # All go to B

        #-----------------------------------------------------------------------
        # This is the instruction-list that the test looks at as various
        # milestones are met during testing. If a given event happens,
        # and if it matches the event in the current step of the instructions,
        # then the test will execute the action in the current step, and
        # advance to the next.
        # These instructions lists make the test more flexible, so I can get
        # different behavior without writing *almost* the same code mutiple
        # times.
        #-----------------------------------------------------------------------

        # note: if 'done' is present in an action, it always means 'succeed now'.
        # If there had been a failure, that would have been caught in an
        # earlier part of ther action.

        instructions = [
                         # Once the link-routable address is ready to use in
                         # the router network, create 4 senders.
                         {
                           'event'  : 'address_ready',
                           'action' : { 'fn'   : 'make_senders',
                                         'arg' : 4
                                      }
                         },
                         # Check the distribution of the first four
                         # link-attach routings, then go immediately
                         # to the next instruction step.
                         {
                           'event'  : 'got_receivers',
                           'action' : { 'fn'   : 'check_receiver_distribution',
                                        'arg'  : first_4
                                      }
                         },
                         # After we see that the first 4 senders have
                         # had their link-attaches routed to the right place,
                         # (which will be router A), close all route-container
                         # connections to that router.
                         {
                           'event'  : 'receiver_distribution_ok',
                           'action' : { 'fn'   : 'kill_connections',
                                        'arg'  : 0
                                      }
                         },
                         # Once the route-container connections on A are
                         # closed, make 4 new senders
                         {
                           'event'  : 'connections_closed',
                           'action' : { 'fn'   : 'make_senders',
                                         'arg' : 4
                                      }
                         },
                         # The link-attaches from these 4 new senders
                         # should now all to the route-container connections
                         # on router B.
                         {
                           'event'  : 'got_receivers',
                           'action' : { 'fn'   : 'check_receiver_distribution',
                                        'arg'  : second_4
                                      }
                         },
                         # If we receive confirmation that the link-attaches
                         # have gone to the right place, we succeed.
                         {
                           'event'  : 'receiver_distribution_ok',
                           'action' : { 'fn'   : 'none',
                                        'done' : 'succeed'
                                      }
                         }
                       ]

        # Tell the test how to check for the address being ready.
        n_local_containers = 0
        # NOTE TODO FIXME -- THIS SHOULD BE 2
        # But if I set it to 2 here, none of the tests will work.
        # This is the first bug to fix^W^W^W functionality improvement opportunity
        # to take advantage of.
        n_remote_routers   = 1

        test = RoutingTest ( self.A_addr,  # all senders are attached here
                             routers,
                             self.linkroute_prefix,
                             addr_suffix,
                             instructions,
                             where_to_make_connections,
                             n_local_containers,
                             n_remote_routers,
                             "Test 18"
                           )
        test.run ( )
        self.assertEqual ( None, test.error )



#================================================================
#     Tests
#================================================================

class TargetedSenderTest ( MessagingHandler ):
    """
    A 'targeted' sender is one in which we tell the router what
    address we want to send to. (As opposed to letting the router
    pass back an address to us.)
    """
    def __init__ ( self, send_addr, recv_addr, destination ):
        super(TargetedSenderTest, self).__init__(prefetch=0)
        self.send_addr  = send_addr
        self.recv_addr  = recv_addr
        self.dest       = destination
        self.error      = None
        self.sender     = None
        self.receiver   = None
        self.n_expected = 10
        self.n_sent     = 0
        self.n_received = 0
        self.n_accepted = 0


    def timeout(self):
        self.error = "Timeout Expired: n_sent=%d n_received=%d n_accepted=%d" % \
                     (self.n_sent, self.n_received, self.n_accepted)
        self.send_conn.close()
        self.recv_conn.close()


    def on_start(self, event):
        self.timer = event.reactor.schedule(TIMEOUT, Timeout(self))
        self.send_conn = event.container.connect(self.send_addr)
        self.recv_conn = event.container.connect(self.recv_addr)
        self.sender   = event.container.create_sender(self.send_conn, self.dest)
        self.receiver = event.container.create_receiver(self.recv_conn, self.dest)
        self.receiver.flow(self.n_expected)


    def send(self):
      while self.sender.credit > 0 and self.n_sent < self.n_expected:
        msg = Message(body=self.n_sent)
        self.sender.send(msg)
        self.n_sent += 1


    def on_sendable(self, event):
        if self.n_sent < self.n_expected:
            self.send()


    def on_accepted(self, event):
        self.n_accepted += 1


    def on_message(self, event):
        self.n_received += 1
        if self.n_received == self.n_expected:
            self.receiver.close()
            self.send_conn.close()
            self.recv_conn.close()
            self.timer.cancel()


    def run(self):
        Container(self).run()



class DynamicTarget(LinkOption):

    def apply(self, link):
        link.target.dynamic = True
        link.target.address = None



class AnonymousSenderTest ( MessagingHandler ):
    """
    An 'anonymous' sender is one in which we let the router tell
    us what address the sender should use.  It will pass back this
    information to us when we get the on_link_opened event.
    """

    def __init__(self, send_addr, recv_addr):
        super(AnonymousSenderTest, self).__init__()
        self.send_addr = send_addr
        self.recv_addr = recv_addr

        self.error     = None
        self.recv_conn = None
        self.send_conn = None
        self.sender    = None
        self.receiver  = None
        self.address   = None

        self.expected   = 10
        self.n_sent     = 0
        self.n_received = 0
        self.n_accepted = 0


    def timeout ( self ):
        self.error = "Timeout Expired: n_sent=%d n_received=%d n_accepted=%d" % \
                     (self.n_sent, self.n_received, self.n_accepted)
        self.send_conn.close()
        self.recv_conn.close()


    def on_start(self, event):
        self.timer     = event.reactor.schedule(TIMEOUT, Timeout(self))
        self.send_conn = event.container.connect(self.send_addr)
        self.recv_conn = event.container.connect(self.recv_addr)
        self.sender    = event.container.create_sender(self.send_conn, options=DynamicTarget())


    def send(self):
        while self.sender.credit > 0 and self.n_sent < self.expected:
            self.n_sent += 1
            m = Message(address=self.address, body="Message %d of %d" % (self.n_sent, self.expected))
            self.sender.send(m)


    def on_link_opened(self, event):
        if event.sender == self.sender:
            # Here we are told the address that we will use for the sender.
            self.address = self.sender.remote_target.address
            self.receiver = event.container.create_receiver(self.recv_conn, self.address)


    def on_sendable(self, event):
        self.send()


    def on_message(self, event):
        if event.receiver == self.receiver:
            self.n_received += 1


    def on_accepted(self, event):
        self.n_accepted += 1
        if self.n_accepted == self.expected:
            self.send_conn.close()
            self.recv_conn.close()
            self.timer.cancel()


    def run(self):
        Container(self).run()





#=======================================================================
#=======================================================================
class DynamicReplyTo(MessagingHandler):
    """
    In this test we have a separate 'client' and 'server' with separate
    connections.  The client sends requests to the server, and embeds in
    them its desired reply-to address.  The server uses that address to
    send back messages.  The tests ends with success if the client receives
    the expected number of replies, or with failure if we time out before
    that happens.
    """
    def __init__(self, client_addr, server_addr):
        super(DynamicReplyTo, self).__init__(prefetch=10)
        self.client_addr        = client_addr
        self.server_addr        = server_addr
        self.dest               = "closest.dynamicRequestResponse"
        self.error              = None
        self.server_receiver    = None
        self.client_receiver    = None
        self.sender             = None
        self.server_sender      = None
        self.n_expected         = 10
        self.n_sent             = 0
        self.received_by_server = 0
        self.received_by_client = 0


    def timeout(self):
        self.error = "Timeout Expired: n_sent=%d received_by_server=%d received_by_client=%d" % \
                     (self.n_sent, self.received_by_server, self.received_by_client)
        self.client_connection.close()
        self.server_connection.close()


    def on_start ( self, event ):
        self.timer             = event.reactor.schedule ( TIMEOUT, Timeout(self) )
        # separate connections to simulate client and server.
        self.client_connection = event.container.connect(self.client_addr)
        self.server_connection = event.container.connect(self.server_addr)

        self.sender            = event.container.create_sender(self.client_connection, self.dest)
        self.server_sender     = event.container.create_sender(self.server_connection, None)

        self.server_receiver   = event.container.create_receiver(self.server_connection, self.dest)
        self.client_receiver   = event.container.create_receiver(self.client_connection, None, dynamic=True)


    def on_sendable(self, event):
        reply_to_addr = self.client_receiver.remote_source.address

        if reply_to_addr == None:
          return

        while event.sender.credit > 0 and self.n_sent < self.n_expected:
            # We send to server, and tell it how to reply to the client.
            request = Message ( body=self.n_sent,
                                address=self.dest,
                                reply_to = reply_to_addr )
            event.sender.send ( request )
            self.n_sent += 1


    def on_message(self, event):
        # Server gets a request and responds to
        # the address that is embedded in the message.
        if event.receiver == self.server_receiver :
            self.server_sender.send ( Message(address=event.message.reply_to,
                                      body="Reply hazy, try again later.") )
            self.received_by_server += 1

        # Client gets a response and counts it.
        elif event.receiver == self.client_receiver :
            self.received_by_client += 1
            if self.received_by_client == self.n_expected:
                self.timer.cancel()
                self.server_receiver.close()
                self.client_receiver.close()
                self.client_connection.close()
                self.server_connection.close()


    def run(self):
        Container(self).run()




class LinkAttachRouting ( MessagingHandler ):
    """
    There are two hosts: near, and far.  The far host is the one that
    the route container will connect to, and it will receive our messages.
    The near host is what our sender will attach to.
    """
    def __init__ ( self, nearside_host, farside_host, linkroute_prefix, addr_suffix ):
        super ( LinkAttachRouting, self ).__init__(prefetch=0)
        self.nearside_host         = nearside_host
        self.farside_host          = farside_host
        self.linkroute_prefix      = linkroute_prefix
        self.link_routable_address = self.linkroute_prefix + '.' + addr_suffix

        self.nearside_cnx             = None
        self.farside_cnx              = None
        self.error                    = None
        self.nearside_sender          = None
        self.farside_receiver         = None
        self.linkroute_check_timer    = None
        self.linkroute_check_receiver = None
        self.linkroute_check_sender   = None

        self.count     = 10
        self.n_sent    = 0
        self.n_rcvd    = 0
        self.n_settled = 0


    def timeout ( self ):
        self.bail ( "Timeout Expired: n_sent=%d n_rcvd=%d n_settled=%d" %
                    (self.n_sent, self.n_rcvd, self.n_settled) )


    def address_check_timeout(self):
        self.linkroute_check()


    def bail ( self, text ):
        self.error = text
        self.farside_cnx.close()
        self.nearside_cnx.close()
        self.timer.cancel()
        if self.linkroute_check_timer:
            self.linkroute_check_timer.cancel()


    def on_start(self, event):
        self.timer        = event.reactor.schedule(TIMEOUT, Timeout(self))
        self.nearside_cnx = event.container.connect(self.nearside_host)

        # Step 1: I make the far cnx.  Once this is done, if we later attach
        # anywhere with a link whose address matches the link-attach routable
        # prefix, the link-attach route will be formed.
        self.farside_cnx = event.container.connect(self.farside_host)

        # Since the route container will be connected to Farside, and
        # my router network is linear, I make the linkroute checker attach
        # to Nearside.
        self.linkroute_check_receiver = event.container.create_receiver(self.nearside_cnx, dynamic=True)
        self.linkroute_check_sender   = event.container.create_sender(self.nearside_cnx, "$management")


    def on_link_opened(self, event):
        if event.receiver:
            event.receiver.flow(self.count)
        if event.receiver == self.linkroute_check_receiver:
            # Step 2. my linkroute check-link has opened: make the linkroute_checker
            self.linkroute_checker = AddressChecker(self.linkroute_check_receiver.remote_source.address)
            self.linkroute_check()


    def on_message(self, event):
        if event.receiver == self.farside_receiver:
            # This is a payload message.
            self.n_rcvd += 1

        elif event.receiver == self.linkroute_check_receiver:
            # This is one of my route-readiness checking messages.
            response = self.linkroute_checker.parse_address_query_response(event.message)
            if response.status_code == 200 and (response.remoteCount + response.containerCount) > 0:
                # Step 3: got confirmation of link-attach knowledge fully propagated
                # to Nearside router.  Now we can make the nearside sender without getting
                # a No Path To Destination error.
                self.nearside_sender = event.container.create_sender(self.nearside_cnx, self.link_routable_address)
                # And we can quit checking.
                if self.linkroute_check_timer:
                    self.linkroute_check_timer.cancel()
                    self.linkroute_check_timer = None
            else:
                # If the latest check did not find the link-attack route ready,
                # schedule another check a little while from now.
                self.linkroute_check_timer = event.reactor.schedule(0.25, AddressCheckerTimeout(self))


    def on_link_opening ( self, event ):
        if event.receiver:
            # Step 4.  At start-up, I connected to the route-container listener on
            # Farside, which makes me the route container.  So when a sender attaches
            # to the network and wants to send to the linkroutable address, the router
            # network creates the link-attach route, and then hands me a receiver for it.
            if event.receiver.remote_target.address == self.link_routable_address:
                self.farside_receiver = event.receiver
                event.receiver.target.address = self.link_routable_address
                event.receiver.open()
            else:
                self.bail("Incorrect address on incoming receiver: got %s, expected %s" %
                          (event.receiver.remote_target.address, self.link_routable_address))


    def on_sendable ( self, event ):
        # Step 5: once there is someone on the network who can receive
        # my messages, I get the go-ahead for my sender.
        if event.sender == self.nearside_sender:
            self.send()


    def send ( self ):
        while self.nearside_sender.credit > 0 and self.n_sent < self.count:
            self.n_sent += 1
            m = Message(body="Message %d of %d" % (self.n_sent, self.count))
            self.nearside_sender.send(m)


    def linkroute_check ( self ):
        # Send the message that will query the management code to discover
        # information about our destination address. We cannot make our payload
        # sender until the network is ready.
        #
        # BUGALERT: We have to prepend the 'D' to this linkroute prefix
        # because that's what the router does internally.  Someday this
        # may change.
        self.linkroute_check_sender.send ( self.linkroute_checker.make_address_query("D" + self.linkroute_prefix) )


    def on_settled(self, event):
        if event.sender == self.nearside_sender:
            self.n_settled += 1
            if self.n_settled == self.count:
                self.bail ( None )


    def run(self):
        container = Container(self)
        container.container_id = 'LinkRouteTest'
        container.run()



class ClosestTest ( MessagingHandler ):
    """
    Test whether distance-based message routing works in a
    3-router network. The network may be linear or mesh,
    depending on which routers the caller gives us.

    (Illustration is a linear network.)

    sender -----> Router_1 -----> Router_2 -----> Router_3
                     |              |                |
                     v              v                v
                  rcvr_1_a       rcvr_2_a         rcvr_3_a
                  rcvr_1_b       rcvr_2_b         rcvr_3_b

    With a linear network of 3 routers, set up a sender on
    router_1, and then 2 receivers each on all 3 routers.

    """
    def __init__ ( self, router_1, router_2, router_3, addr_suffix ):
        super ( ClosestTest, self ).__init__(prefetch=0)
        self.error       = None
        self.router_1    = router_1
        self.router_2    = router_2
        self.router_3    = router_3
        self.addr_suffix = addr_suffix
        self.dest        = "closest/" + addr_suffix

        # This n_expected is actually the minimum number of messages
        # I will send.  The real number will be higher because some
        # will be released when I close some receivers.
        self.n_expected = 300
        self.one_third  = self.n_expected / 3

        self.n_received = 0

        self.count_1_a = 0
        self.count_1_b = 0
        self.count_2_a = 0
        self.count_2_b = 0
        self.count_3_a = 0
        self.count_3_b = 0

        self.addr_check_timer    = None
        self.addr_check_receiver = None
        self.addr_check_sender   = None
        self.bailed = False

    def timeout ( self ):
        self.bail ( "Timeout Expired " )


    def address_check_timeout(self):
        self.addr_check()


    def bail ( self, text ):
        self.timer.cancel()
        self.error = text
        self.send_cnx.close()
        self.cnx_1.close()
        self.cnx_2.close()
        self.cnx_3.close()
        if self.addr_check_timer:
            self.addr_check_timer.cancel()


    def on_start ( self, event ):
        self.timer    = event.reactor.schedule  ( TIMEOUT, Timeout(self) )
        self.send_cnx = event.container.connect ( self.router_1 )
        self.cnx_1    = event.container.connect ( self.router_1 )
        self.cnx_2    = event.container.connect ( self.router_2 )
        self.cnx_3    = event.container.connect ( self.router_3 )

        # Warning!
        # The two receiver-links on each router must be given
        # explicit distinct names, or we will in fact only get
        # one link.  And then wonder why receiver 2 on each
        # router isn't getting any messages.
        self.recv_1_a  = event.container.create_receiver  ( self.cnx_1, self.dest, name="1" )
        self.recv_1_b  = event.container.create_receiver  ( self.cnx_1, self.dest, name="2" )

        self.recv_2_a  = event.container.create_receiver  ( self.cnx_2,  self.dest, name="1" )
        self.recv_2_b  = event.container.create_receiver  ( self.cnx_2,  self.dest, name="2" )

        self.recv_3_a  = event.container.create_receiver  ( self.cnx_3,  self.dest, name="1" )
        self.recv_3_b  = event.container.create_receiver  ( self.cnx_3,  self.dest, name="2" )

        self.recv_1_a.flow ( self.n_expected )
        self.recv_2_a.flow ( self.n_expected )
        self.recv_3_a.flow ( self.n_expected )

        self.recv_1_b.flow ( self.n_expected )
        self.recv_2_b.flow ( self.n_expected )
        self.recv_3_b.flow ( self.n_expected )

        self.addr_check_receiver = event.container.create_receiver ( self.cnx_1, dynamic=True )
        self.addr_check_sender   = event.container.create_sender ( self.cnx_1, "$management" )


    def on_link_opened(self, event):
        if event.receiver:
            event.receiver.flow ( self.n_expected )
        if event.receiver == self.addr_check_receiver:
            # my addr-check link has opened: make the addr_checker with the given address.
            self.addr_checker = AddressChecker ( self.addr_check_receiver.remote_source.address )
            self.addr_check()


    def on_sendable ( self, event ):
        msg = Message ( body     = "Hello, closest.",
                        address  = self.dest
                      )
        event.sender.send ( msg )


    def on_message ( self, event ):

        if event.receiver == self.addr_check_receiver:
            # This is a response to one of my address-readiness checking messages.
            response = self.addr_checker.parse_address_query_response(event.message)
            if response.status_code == 200 and response.subscriberCount == 2 and response.remoteCount == 2:
                # now we know that we have two subscribers on attached router, and two remote
                # routers that know about the address. The network is ready.
                # Now we can make the sender without getting a
                # "No Path To Destination" error.
                self.sender = event.container.create_sender ( self.send_cnx, self.dest )

                # And we can quit checking.
                if self.addr_check_timer:
                    self.addr_check_timer.cancel()
                    self.addr_check_timer = None
            else:
                # If the latest check did not find the link-attack route ready,
                # schedule another check a little while from now.
                self.addr_check_timer = event.reactor.schedule(0.25, AddressCheckerTimeout(self))
        else :
            # This is a payload message.
            self.n_received += 1

            # Count the messages that have come in for
            # each receiver.
            if event.receiver == self.recv_1_a:
                self.count_1_a += 1
            elif event.receiver == self.recv_1_b:
                self.count_1_b += 1
            elif event.receiver == self.recv_2_a:
                self.count_2_a += 1
            elif event.receiver == self.recv_2_b:
                self.count_2_b += 1
            elif event.receiver == self.recv_3_a:
                self.count_3_a += 1
            elif event.receiver == self.recv_3_b:
                self.count_3_b += 1

            if self.n_received == self.one_third:
                # The first one-third of messages should have gone exclusively
                # to the near receivers.  At this point we should have
                # no messages in the mid or far receivers.
                self.recv_1_a.close()
                self.recv_1_b.close()
                if (self.count_2_a + self.count_2_b + self.count_3_a + self.count_3_b) > 0 :
                    self.bail ( "error: routers 2 or 3 got messages before router 1 receivers were closed." )
                # Make sure both receivers got some messages.
                if (self.count_1_a * self.count_1_b) == 0:
                    self.bail ( "error: one of the receivers on router 1 got no messages." )

            elif self.n_received == 2 * self.one_third:
                # The next one-third of messages should have gone exclusively
                # to the router_2 receivers.  At this point we should have
                # no messages in the far receivers.
                self.recv_2_a.close()
                self.recv_2_b.close()
                if (self.count_3_a + self.count_3_b) > 0 :
                    self.bail ( "error: router 3 got messages before 2 was closed." )
                # Make sure both receivers got some messages.
                if (self.count_2_a * self.count_2_b) == 0:
                    self.bail ( "error: one of the receivers on router 2 got no messages." )

            # By the time we reach the expected number of messages
            # we have closed the router_1 and router_2 receivers.  If the
            # router_3 receivers are empty at this point, something is wrong.
            if self.n_received >= self.n_expected :
                if (self.count_3_a * self.count_3_b) == 0:
                    self.bail ( "error: one of the receivers on router 3 got no messages." )
                else:
                    self.bail ( None )


    def addr_check ( self ):
        # Send the message that will query the management code to discover
        # information about our destination address. We cannot make our payload
        # sender until the network is ready.
        #
        # BUGALERT: We have to prepend the 'M0' to this address prefix
        # because that's what the router does internally.  Someday this
        # may change.
        self.addr_check_sender.send ( self.addr_checker.make_address_query("M0" + self.dest) )


    def run(self):
        container = Container(self)
        container.run()





class BalancedTest ( MessagingHandler ):
    """
    This test is topology-agnostic. This code thinks of its nodes as 1, 2, 3.
    The caller knows if they are linear or triangular, or a tree.  It calculates
    the expected results for nodes 1, 2, and 3, and also tells me if there can be
    a little 'slop' in the results.
    ( Slop can happen in some topologies when you can't tell whether spillover
    will happen first to node 2, or to node 3.
    """
    def __init__ ( self, router_1, router_2, router_3, addr_suffix, total_messages, expected_1, expected_2, expected_3, slop, omit_middle_receiver ):
        super ( BalancedTest, self ).__init__(prefetch=0, auto_accept=False)
        self.error       = None
        self.router_3    = router_3
        self.router_2    = router_2
        self.router_1    = router_1
        self.addr_suffix = addr_suffix
        self.dest        = "balanced/" + addr_suffix

        self.total_messages  = total_messages
        self.n_sent          = 0
        self.n_received      = 0

        self.recv_1 = None
        self.recv_2 = None
        self.recv_3 = None

        self.count_3 = 0
        self.count_2 = 0
        self.count_1 = 0

        self.expected_1 = expected_1
        self.expected_2 = expected_2
        self.expected_3 = expected_3
        self.slop       = slop
        self.omit_middle_receiver = omit_middle_receiver

        self.address_check_timer    = None
        self.address_check_receiver = None
        self.address_check_sender   = None

        self.payload_sender = None


    def timeout ( self ):
        self.bail ( "Timeout Expired " )


    def address_check_timeout(self):
        self.address_check()


    def bail ( self, text ):
        self.timer.cancel()
        self.error = text
        self.cnx_3.close()
        self.cnx_2.close()
        self.cnx_1.close()
        if self.address_check_timer:
            self.address_check_timer.cancel()


    def on_start ( self, event ):
        self.timer    = event.reactor.schedule  ( TIMEOUT, Timeout(self) )
        self.cnx_3    = event.container.connect ( self.router_3 )
        self.cnx_2    = event.container.connect ( self.router_2 )
        self.cnx_1    = event.container.connect ( self.router_1 )

        self.recv_3  = event.container.create_receiver ( self.cnx_3,  self.dest )
        if self.omit_middle_receiver is False :
            self.recv_2 = event.container.create_receiver ( self.cnx_2,  self.dest )
        self.recv_1  = event.container.create_receiver ( self.cnx_1,  self.dest )

        self.recv_3.flow ( self.total_messages )
        if self.omit_middle_receiver is False :
            self.recv_2.flow ( self.total_messages )
        self.recv_1.flow ( self.total_messages )

        self.address_check_receiver = event.container.create_receiver ( self.cnx_1, dynamic=True )
        self.address_check_sender   = event.container.create_sender   ( self.cnx_1, "$management" )


    def on_link_opened(self, event):
        if event.receiver:
            event.receiver.flow ( self.total_messages )
        if event.receiver == self.address_check_receiver:
            # My address check-link has opened: make the address_checker
            self.address_checker = AddressChecker ( self.address_check_receiver.remote_source.address )
            self.address_check()


    def on_message ( self, event ):

        if self.n_received >= self.total_messages:
            return   # Sometimes you can get a message or two even after you have called bail().

        if event.receiver == self.address_check_receiver:
            # This is one of my route-readiness checking messages.
            response = self.address_checker.parse_address_query_response(event.message)
            if self.omit_middle_receiver is True :
                expected_remotes = 1
            else :
                expected_remotes = 2

            if response.status_code == 200 and response.subscriberCount == 1 and response.remoteCount == expected_remotes:
                # Got confirmation of dest addr fully propagated through network.
                # Since I have 3 nodes, I want to see 1 subscriber (which is on the local router) and
                # 2 remote routers that know about my destination address.
                # Now we can safely make the payload sender without getting a 'No Path To Destination' error.
                self.payload_sender = event.container.create_sender ( self.cnx_1, self.dest )
                # And we can quit checking.
                if self.address_check_timer:
                    self.address_check_timer.cancel()
                    self.address_check_timer = None
            else:
                # If the latest check did not find the link-attack route ready,
                # schedule another check a little while from now.
                self.address_check_timer = event.reactor.schedule(0.50, AddressCheckerTimeout(self))

        else:
            self.n_received += 1

            if   event.receiver == self.recv_1: self.count_1 += 1
            elif event.receiver == self.recv_2: self.count_2 += 1
            elif event.receiver == self.recv_3: self.count_3 += 1

            # I do not check for count_1 + count_2 + count_3 == total,
            # because it always will be due to how the code counts things.
            if self.n_received == self.total_messages:
                if self.count_1 != self.expected_1:
                    self.bail ( "bad count 1: count %d != expected %d" % (self.count_1, self.expected_1) )
                elif abs(self.count_2 - self.expected_2) > self.slop:
                    self.bail ( "count_2 %d is more than %d different from expectation %d" % (self.count_2, self.slop, self.expected_2) )
                elif abs(self.count_3 - self.expected_3) > self.slop:
                    self.bail ( "count_3 %d is more than %d different from expectation %d" % (self.count_3, self.slop, self.expected_3) )
                else:
                    self.bail ( None) # All is well.


    def on_sendable ( self, event ):
        if self.n_sent < self.total_messages and event.sender == self.payload_sender :
            msg = Message ( body     = "Hello, balanced.",
                            address  = self.dest
                          )
            self.payload_sender.send ( msg )
            self.n_sent += 1


    def address_check ( self ):
        # Send the message that will query the management code to discover
        # information about our destination address. We cannot make our payload
        # sender until the network is ready.
        #
        # BUGALERT: We have to prepend the 'M0' to this address prefix
        # because that's what the router does internally.  Someday this
        # may change.
        self.address_check_sender.send ( self.address_checker.make_address_query("M0" + self.dest) )


    def run(self):
        container = Container(self)
        container.run()





class MulticastTest ( MessagingHandler ):
    """
    Using multicast, we should see all receivers get everything,
    whether the topology is linear or mesh.
    """
    def __init__ ( self, router_1, router_2, router_3, addr_suffix ):
        super ( MulticastTest, self ).__init__(prefetch=0)
        self.error       = None
        self.router_1    = router_1
        self.router_2    = router_2
        self.router_3    = router_3
        self.addr_suffix = addr_suffix
        self.dest        = "multicast/" + addr_suffix

        self.n_to_send = 100
        self.n_sent    = 0

        self.n_received = 0

        self.count_1_a = 0
        self.count_1_b = 0
        self.count_2_a = 0
        self.count_2_b = 0
        self.count_3_a = 0
        self.count_3_b = 0

        self.addr_check_timer    = None
        self.addr_check_receiver = None
        self.addr_check_sender   = None
        self.sender              = None
        self.bailed = False

    def timeout ( self ):
        self.check_results ( )
        self.bail ( "Timeout Expired " )


    def address_check_timeout(self):
        self.addr_check()


    def bail ( self, text ):
        self.timer.cancel()
        self.error = text
        self.send_cnx.close()
        self.cnx_1.close()
        self.cnx_2.close()
        self.cnx_3.close()
        if self.addr_check_timer:
            self.addr_check_timer.cancel()


    def on_start ( self, event ):
        self.timer    = event.reactor.schedule  ( TIMEOUT, Timeout(self) )
        self.send_cnx = event.container.connect ( self.router_1 )
        self.cnx_1    = event.container.connect ( self.router_1 )
        self.cnx_2    = event.container.connect ( self.router_2 )
        self.cnx_3    = event.container.connect ( self.router_3 )

        # Warning!
        # The two receiver-links on each router must be given
        # explicit distinct names, or we will in fact only get
        # one link.  And then wonder why receiver 2 on each
        # router isn't getting any messages.
        self.recv_1_a  = event.container.create_receiver  ( self.cnx_1, self.dest, name="1" )
        self.recv_1_b  = event.container.create_receiver  ( self.cnx_1, self.dest, name="2" )

        self.recv_2_a  = event.container.create_receiver  ( self.cnx_2,  self.dest, name="1" )
        self.recv_2_b  = event.container.create_receiver  ( self.cnx_2,  self.dest, name="2" )

        self.recv_3_a  = event.container.create_receiver  ( self.cnx_3,  self.dest, name="1" )
        self.recv_3_b  = event.container.create_receiver  ( self.cnx_3,  self.dest, name="2" )

        self.recv_1_a.flow ( self.n_to_send )
        self.recv_2_a.flow ( self.n_to_send )
        self.recv_3_a.flow ( self.n_to_send )

        self.recv_1_b.flow ( self.n_to_send )
        self.recv_2_b.flow ( self.n_to_send )
        self.recv_3_b.flow ( self.n_to_send )

        self.addr_check_receiver = event.container.create_receiver ( self.cnx_1, dynamic=True )
        self.addr_check_sender   = event.container.create_sender ( self.cnx_1, "$management" )


    def on_link_opened(self, event):
        if event.receiver:
            event.receiver.flow ( self.n_to_send )
        if event.receiver == self.addr_check_receiver:
            # my addr-check link has opened: make the addr_checker with the given address.
            self.addr_checker = AddressChecker ( self.addr_check_receiver.remote_source.address )
            self.addr_check()


    def on_sendable ( self, event ):
        if self.sender and self.n_sent < self.n_to_send :
            msg = Message ( body     = "Hello, closest.",
                            address  = self.dest
                          )
            dlv = self.sender.send ( msg )
            self.n_sent += 1
            dlv.settle()


    def on_message ( self, event ):

        #if self.bailed is True :
            #return

        if event.receiver == self.addr_check_receiver:
            # This is a response to one of my address-readiness checking messages.
            response = self.addr_checker.parse_address_query_response(event.message)
            if response.status_code == 200 and response.subscriberCount == 2 and response.remoteCount == 2:
                # now we know that we have two subscribers on attached router, and two remote
                # routers that know about the address. The network is ready.
                # Now we can make the sender without getting a
                # "No Path To Destination" error.
                self.sender = event.container.create_sender ( self.send_cnx, self.dest )

                # And we can quit checking.
                if self.addr_check_timer:
                    self.addr_check_timer.cancel()
                    self.addr_check_timer = None
            else:
                # If the latest check did not find the link-attack route ready,
                # schedule another check a little while from now.
                self.addr_check_timer = event.reactor.schedule(0.25, AddressCheckerTimeout(self))
        else :
            # This is a payload message.
            self.n_received += 1

            # Count the messages that have come in for
            # each receiver.
            if   event.receiver == self.recv_1_a:
                self.count_1_a += 1
            elif event.receiver == self.recv_1_b:
                self.count_1_b += 1
            elif event.receiver == self.recv_2_a:
                self.count_2_a += 1
            elif event.receiver == self.recv_2_b:
                self.count_2_b += 1
            elif event.receiver == self.recv_3_a:
                self.count_3_a += 1
            elif event.receiver == self.recv_3_b:
                self.count_3_b += 1

            if self.n_received >= 6 * self.n_to_send :
                # In multicast, everybody gets everything.
                # Our reception count should be 6x our send-count,
                # and all receiver-counts should be equal.
                if self.count_1_a == self.count_1_b and self.count_1_b == self.count_2_a and self.count_2_a == self.count_2_b and self.count_2_b == self.count_3_a and self.count_3_a == self.count_3_b :
                    self.bail ( None )
                    self.bailed = True
                else:
                    self.bail ( "receivers not equal: %d %d %d %d %d %d" % (self.count_1_a, self.count_1_b, self.count_2_a, self.count_2_b, self.count_3_a, self.count_3_b) )
                    self.bailed = True



    def addr_check ( self ):
        # Send the message that will query the management code to discover
        # information about our destination address. We cannot make our payload
        # sender until the network is ready.
        #
        # BUGALERT: We have to prepend the 'M0' to this address prefix
        # because that's what the router does internally.  Someday this
        # may change.
        self.addr_check_sender.send ( self.addr_checker.make_address_query("M0" + self.dest) )


    def run(self):
        container = Container(self)
        container.run()





class RoutingTest ( MessagingHandler ):
    """
    Accept a network of three routers -- either linear or triangle,
    depending on what the caller chooses -- make some senders, and see
    where the tests go. This test may also kill some connections, make
    some more sewnders, and then see where *their* link-attaches get
    routed. This test's exact behavior is determined by the list of
    instructions that are passed in by the caller, each instruction being
    executed when some milestone in the test is met.

    NOTE that no payload messages are sent in this test! I send some
    management messages to see when the router network is ready for me,
    but other than that, all I care about is the link-attaches that happen
    each time I make a sender -- and where they are routed to.
    """
    def __init__ ( self,
                   sender_host,
                   route_container_addrs,
                   linkroute_prefix,
                   addr_suffix,
                   instructions,
                   where_to_make_connections,
                   n_local_containers,
                   n_remote_routers,
                   test_name
                 ):
        super ( RoutingTest, self ).__init__(prefetch=0)

        self.debug     = False
        self.test_name = test_name

        self.sender_host           = sender_host
        self.route_container_addrs = route_container_addrs
        self.linkroute_prefix      = linkroute_prefix
        self.link_routable_address = self.linkroute_prefix + '.' + addr_suffix

        self.instructions = instructions
        self.current_step_index = 0

        self.where_to_make_connections = where_to_make_connections
        self.sender_cnx                = None
        self.error                     = None
        self.linkroute_check_timer     = None
        self.linkroute_check_receiver  = None
        self.linkroute_check_sender    = None

        # These numbers tell me how to know when the
        # link-attach routable address is ready to use
        # in the router network.
        self.n_local_containers = n_local_containers
        self.n_remote_routers   = n_remote_routers

        self.receiver_count     = 0
        self.connections_closed = 0
        self.connections_to_be_closed = 0
        self.expected_receivers = 0
        self.done               = False
        self.my_senders         = []

        # This list of dicts stores the number of route-container
        # connections that have been made to each of the three routers.
        # Each dict will hold one of these:
        #    < cnx : receiver_count >
        # for each cnx on that router.
        self.router_cnx_counts = [ dict(), dict(), dict() ]
        self.cnx_status        = dict()


    def debug_print ( self, message ) :
        if self.debug :
            print message


    # Some places in the test generate their own testing-events
    # or testing milestones, and call this fn.  If the event
    # corresponds to the one in the current 'step' of the
    # instructions from the caller, this function will perform
    # some action, and advance to the next step.
    # It's a simple, linear state machine, to make this test more
    # flexible.
    def execute_next_instruction ( self, event ):

        if self.current_step_index == len(self.instructions) :
            self.debug_print ( "All done bailing out." )
            self.bail ( None )
            return

        current_step = self.instructions [ self.current_step_index ]

        # If the test-milestone event that the caller passed in
        # matches the next one on the list, execute the associated
        # action and advance the current step.
        # If the associated action is a check, and if that check fails,
        # then the check code itself will end this test in failure.
        # For the test to succeed, we must reach the final step of
        # the instructions, which must be an explicit 'succeed' step.
        if event == current_step['event'] :
            self.current_step_index += 1
            self.debug_print ( "\nexecute_next_instruction: %s" % event )
            action = current_step['action']

            self.debug_print ( "    action['fn'] == %s" % action['fn'] )

            # Each step of the instructions has an associated
            # action, which is a function to call, and maybe
            # an argument to pass to it.
            if action['fn'] == 'make_senders' :
                arg = int(action['arg'])
                self.make_senders ( arg )
                self.expected_receivers = arg
                self.receiver_count = 0
            elif action['fn'] == 'check_receiver_distribution' :
                error = self.check_router_cnx_receiver_count ( action['arg'] )
                if error :
                    self.debug_print ( "check_router_cnx_receiver_count error" )
                    self.done = True
                    self.bail ( error )
                    return
                else:
                    self.debug_print ( "receiver_distribution_ok" )
                    self.execute_next_instruction ( 'receiver_distribution_ok' )
            elif action['fn'] == 'kill_connections' :
                self.connections_to_be_closed = 2
                self.connections_closed       = 0
                self.close_route_container_connections_on_router_n ( action['arg'] )
            elif action['fn'] == 'none' :
                if 'done' in action:
                    # This is the final instruction on the list
                    # that tells us explicitly to terminate
                    # with success.
                    self.debug_print ( "done -- succeeding." )
                    self.bail ( None )


    # If this happens, the test is hanging.
    def timeout ( self ):
        self.bail ( "Timeout Expired" )


    # This helps us periodically send management queries
    # to learn when our address os ready to be used on the
    # router network.
    def address_check_timeout(self):
        self.linkroute_check()


    def bail ( self, text ):
        self.done = True
        self.error = text
        self.close_route_container_connections()
        self.sender_cnx.close()
        self.timer.cancel()
        if self.linkroute_check_timer:
            self.linkroute_check_timer.cancel()


    def on_start ( self, event ):

        self.debug_print ( "\n\n%s ===========================================\n\n" % self.test_name )
        self.timer = event.reactor.schedule ( TIMEOUT, Timeout(self) )
        self.sender_cnx = event.container.connect(self.sender_host)

        # Instructions from on high tell us how many route-container
        # connections to make on each router. For each one that we
        # make, we store it in a dict for that router, and associate
        # the number 0 with it. That number will be incremented every
        # time that connection is awarded a receiver. (Every time it
        # gets a sender's link-attach routed to it.)
        for router in range(len(self.where_to_make_connections)) :
            how_many_for_this_router = self.where_to_make_connections[router]
            for j in range(how_many_for_this_router) :
              route_container_addr = self.route_container_addrs[router]
              cnx = event.container.connect ( route_container_addr )
              # In the dict of connections and actual receiver
              # counts, store this cnx, and 0.
              self.router_cnx_counts[router][cnx] = 0
              self.cnx_status[cnx] = 1
              self.debug_print ( "on_start: made cnx %s on router %d" % ( str(cnx), router ) )

        self.linkroute_check_receiver = event.container.create_receiver ( self.sender_cnx, dynamic=True )
        self.linkroute_check_sender   = event.container.create_sender   ( self.sender_cnx, "$management" )


    #-----------------------------------------------------
    # Check the count of how many receivers came in for
    # each connection compared to what was expected.
    #-----------------------------------------------------
    def check_router_cnx_receiver_count ( self, expected_receiver_counts ) :
        for router in range(len(self.router_cnx_counts)) :
            cnx_dict = self.router_cnx_counts[router]
            # Sum up all receivers for this router.
            actual = 0
            for cnx in cnx_dict :
                receiver_count = cnx_dict[cnx]
                actual += receiver_count

            expected = expected_receiver_counts[router]
            if actual != expected :
                return "router %d -- expected %d -- actual %d" % (router, expected, actual)
            else :
                self.debug_print ( "check_router_cnx_receiver_count: good: router %d expected: %d actual: %d" % (router, expected, actual) )
            router += 1
        return None


    def close_route_container_connections ( self ) :
        for router in range(len(self.router_cnx_counts)) :
            cnx_dict = self.router_cnx_counts[router]
            for cnx in cnx_dict :
                if self.cnx_status[cnx] :
                    cnx.close()


    def close_route_container_connections_on_router_n ( self, n ) :
        self.debug_print ( "close_route_container_connections_on_router_n %d" % n )
        cnx_dict = self.router_cnx_counts[n]
        for cnx in cnx_dict :
            if self.cnx_status[cnx] :
                cnx.close()


    # When a new receiver is handed to us (because a link-attach from a
    # sender has been routed to one of our route-container connections)
    # increment the number associated with that connection.
    # Also indicate to the caller whether this was indeed one of the
    # route-container connections that we made.
    def increment_router_cnx_receiver_count ( self, new_cnx ) :
        for router in range(len(self.router_cnx_counts)) :
            cnx_dict = self.router_cnx_counts[router]
            for cnx in cnx_dict :
                if cnx == new_cnx :
                    # This cnx has been awarded a new receiver.
                    cnx_dict[cnx] += 1
                    return True
        return False


    def this_is_one_of_my_connections ( self, test_cnx ) :
        for router in range(len((self.router_cnx_counts))) :
            cnx_dict = self.router_cnx_counts[router]
            for cnx in cnx_dict :
                if cnx == test_cnx :
                    return True
        return False


    def on_link_opened ( self, event ):
        if self.done :
          return

        if event.receiver:
            if event.receiver == self.linkroute_check_receiver:
                # If the linkroute readiness checker can't strike oil in 30
                # tries, we are seriously out of luck, and will soon time out.
                event.receiver.flow ( 30 )

        if event.receiver == self.linkroute_check_receiver:
            self.linkroute_checker = AddressChecker(self.linkroute_check_receiver.remote_source.address)
            self.linkroute_check()
        else :
          if event.receiver :
              this_is_one_of_mine = self.increment_router_cnx_receiver_count ( event.receiver.connection )
              if this_is_one_of_mine :
                  self.receiver_count += 1
                  if self.receiver_count == self.expected_receivers :
                    self.execute_next_instruction ( 'got_receivers' )


    def on_connection_closed ( self, event ):
        if self.this_is_one_of_my_connections ( event.connection ) :
            self.cnx_status[event.connection] = 0
            self.connections_closed += 1
            if self.connections_to_be_closed :
                self.debug_print ( "on_connection_closed : %d of %d closed : %s" % (self.connections_closed, self.connections_to_be_closed, str(event.connection)) )
                if self.connections_closed == self.connections_to_be_closed :
                    # Reset both of these counters here, because
                    # they are only used each time we get a 'close connections'
                    # instruction, to jkeep track of its progress.
                    self.connections_to_be_closed = 0
                    self.cconnections_closed      = 0
                    self.execute_next_instruction ( 'connections_closed' )


    #-------------------------------------------------
    # All senders get attached to the first router.
    #-------------------------------------------------
    def make_senders ( self, n ):
        self.debug_print ( "making %d senders" % n )
        for i in xrange(n):
            sender_name = "sender_A_%d" % len ( self.my_senders )
            sender = self.sender_container.create_sender ( self.sender_cnx,
                                                           self.link_routable_address,
                                                           name=sender_name
                                                         )
            self.my_senders.append ( sender )


    #-----------------------------------------------------------------
    # The only messages I care about in this test are the management
    # ones I send to determine when the router network is ready
    # to start routing my sender-attaches.
    #-----------------------------------------------------------------
    def on_message ( self, event ):
        if event.receiver == self.linkroute_check_receiver:
            response = self.linkroute_checker.parse_address_query_response ( event.message )
            if response.status_code == 200                        and \
               response.containerCount >= self.n_local_containers and \
               response.remoteCount >= self.n_remote_routers :
                # We can quit checking now.
                if self.linkroute_check_timer:
                    self.linkroute_check_timer.cancel()
                    self.linkroute_check_timer = None
                self.sender_container = event.container
                self.execute_next_instruction ( 'address_ready' )
            else:
                # If the latest check did not find the link-attach route ready,
                # schedule another check a little while from now.
                self.linkroute_check_timer = event.reactor.schedule ( 1.00, AddressCheckerTimeout(self))


    def linkroute_check ( self ):
        # Send the message that will query the management code to discover
        # information about our destination address. We cannot make our payload
        # sender until the network is ready.
        #
        # BUGALERT: We have to prepend the 'D' to this linkroute prefix
        # because that's what the router does internally.  Someday this
        # may change.
        self.linkroute_check_sender.send ( self.linkroute_checker.make_address_query("D" + self.linkroute_prefix) )


    def run(self):
        container = Container(self)
        container.container_id = 'LinkRouteTest'
        container.run()




if __name__ == '__main__':
    unittest.main(main_module())
