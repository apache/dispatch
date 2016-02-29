/*
 * Licensed to the Apache Software Foundation (ASF) under one
 * or more contributor license agreements.  See the NOTICE file
 * distributed with this work for additional information
 * regarding copyright ownership.  The ASF licenses this file
 * to you under the Apache License, Version 2.0 (the
 * "License"); you may not use this file except in compliance
 * with the License.  You may obtain a copy of the License at
 *
 *   http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing,
 * software distributed under the License is distributed on an
 * "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
 * KIND, either express or implied.  See the License for the
 * specific language governing permissions and limitations
 * under the License.
 */

#include "router_core_private.h"
#include <qpid/dispatch/amqp.h>
#include <stdio.h>

//
// NOTE: If the in_delivery argument is NULL, the resulting out deliveries
//       shall be pre-settled.
//
typedef int (*qdr_forward_message_t) (qdr_core_t      *core,
                                      qdr_address_t   *addr,
                                      qd_message_t    *msg,
                                      qdr_delivery_t  *in_delivery,
                                      bool             exclude_inprocess,
                                      bool             control,
                                      qd_bitmask_t    *link_exclusion);

typedef void (*qdr_forward_attach_t) (qdr_core_t      *core,
                                      qdr_forwarder_t *forw,
                                      qdr_link_t      *link);

struct qdr_forwarder_t {
    qdr_forward_message_t forward_message;
    qdr_forward_attach_t  forward_attach;
    bool                  bypass_valid_origins;
};

//==================================================================================
// Built-in Forwarders
//==================================================================================


qdr_delivery_t *qdr_forward_new_delivery_CT(qdr_core_t *core, qdr_delivery_t *peer, qdr_link_t *link, qd_message_t *msg)
{
    qdr_delivery_t *dlv = new_qdr_delivery_t();

    ZERO(dlv);
    dlv->link    = link;
    dlv->msg     = qd_message_copy(msg);
    dlv->settled = !peer || peer->settled;
    dlv->tag     = core->next_tag++;

    //
    // Create peer linkage only if the delivery is not settled
    //
    if (!dlv->settled) {
        dlv->peer = peer;
        if (peer && peer->peer == 0)
            peer->peer = dlv;  // TODO - make this a back-list for multicast tracking
    }

    return dlv;
}


void qdr_forward_deliver_CT(qdr_core_t *core, qdr_link_t *link, qdr_delivery_t *dlv)
{
    sys_mutex_lock(link->conn->work_lock);
    DEQ_INSERT_TAIL(link->undelivered, dlv);

    //
    // If the link isn't already on the links_with_deliveries list, put it there.
    //
    qdr_add_link_ref(&link->conn->links_with_deliveries, link, QDR_LINK_LIST_CLASS_DELIVERY);
    sys_mutex_unlock(link->conn->work_lock);

    //
    // Activate the outgoing connection for later processing.
    //
    qdr_connection_activate_CT(core, link->conn);
}


void qdr_forward_on_message(qdr_core_t *core, qdr_general_work_t *work)
{
    work->on_message(work->on_message_context, work->msg, work->maskbit);
    qd_message_free(work->msg);
}


void qdr_forward_on_message_CT(qdr_core_t *core, qdr_subscription_t *sub, qdr_link_t *link, qd_message_t *msg)
{
    qdr_general_work_t *work = qdr_general_work(qdr_forward_on_message);
    work->on_message         = sub->on_message;
    work->on_message_context = sub->on_message_context;
    work->msg                = qd_message_copy(msg);
    work->maskbit            = link ? link->conn->mask_bit : 0;
    qdr_post_general_work_CT(core, work);
}


int qdr_forward_multicast_CT(qdr_core_t      *core,
                             qdr_address_t   *addr,
                             qd_message_t    *msg,
                             qdr_delivery_t  *in_delivery,
                             bool             exclude_inprocess,
                             bool             control,
                             qd_bitmask_t    *link_exclusion)
{
    bool bypass_valid_origins = addr->forwarder->bypass_valid_origins;
    int  fanout = 0;

    //
    // Forward to local subscribers
    //
    if (!addr->local || exclude_inprocess) {
        qdr_link_ref_t *link_ref = DEQ_HEAD(addr->rlinks);
        while (link_ref) {
            qdr_link_t     *out_link     = link_ref->link;
            qdr_delivery_t *out_delivery = qdr_forward_new_delivery_CT(core, in_delivery, out_link, msg);
            qdr_forward_deliver_CT(core, out_link, out_delivery);
            fanout++;
            if (out_link->link_type != QD_LINK_CONTROL && out_link->link_type != QD_LINK_ROUTER)
                addr->deliveries_egress++;
            link_ref = DEQ_NEXT(link_ref);
        }
    }

    //
    // Forward to remote routers with subscribers using the appropriate
    // link for the traffic class: control or data
    //
    //
    // Get the mask bit associated with the ingress router for the message.
    // This will be compared against the "valid_origin" masks for each
    // candidate destination router.
    //
    int origin = -1;
    qd_field_iterator_t *ingress_iter = in_delivery ? in_delivery->origin : 0;

    if (ingress_iter && !bypass_valid_origins) {
        qd_address_iterator_reset_view(ingress_iter, ITER_VIEW_NODE_HASH);
        qdr_address_t *origin_addr;
        qd_hash_retrieve(core->addr_hash, ingress_iter, (void*) &origin_addr);
        if (origin_addr && qd_bitmask_cardinality(origin_addr->rnodes) == 1)
            qd_bitmask_first_set(origin_addr->rnodes, &origin);
    } else
        origin = 0;

    //
    // Forward to the next-hops for remote destinations.
    //
    if (origin >= 0) {
        int           dest_bit;
        qdr_link_t   *dest_link;
        qdr_node_t   *next_node;
        qd_bitmask_t *link_set = qd_bitmask(0);

        //
        // Loop over the target nodes for this address.  Build a set of outgoing links
        // for which there are valid targets.  We do this to avoid sending more than one
        // message down a given link.  It's possible that there are multiple destinations
        // for this address that are all reachable over the same link.  In this case, we
        // will send only one copy of the message over the link and allow a downstream
        // router to fan the message out.
        //
        int c;
        for (QD_BITMASK_EACH(addr->rnodes, dest_bit, c)) {
            qdr_node_t *rnode = core->routers_by_mask_bit[dest_bit];
            if (!rnode)
                continue;

            if (rnode->next_hop)
                next_node = rnode->next_hop;
            else
                next_node = rnode;

            dest_link = control ? next_node->peer_control_link : next_node->peer_data_link;
            if (dest_link && qd_bitmask_value(rnode->valid_origins, origin))
                qd_bitmask_set_bit(link_set, dest_link->conn->mask_bit);
        }

        //
        // Send a copy of the message outbound on each identified link.
        //
        int link_bit;
        while (qd_bitmask_first_set(link_set, &link_bit)) {
            qd_bitmask_clear_bit(link_set, link_bit);
            dest_link = control ?
                core->control_links_by_mask_bit[link_bit] :
                core->data_links_by_mask_bit[link_bit];
            if (dest_link && (!link_exclusion || qd_bitmask_value(link_exclusion, link_bit) == 0)) {
                qdr_delivery_t *out_delivery = qdr_forward_new_delivery_CT(core, in_delivery, dest_link, msg);
                qdr_forward_deliver_CT(core, dest_link, out_delivery);
                fanout++;
                addr->deliveries_transit++;
            }
        }

        qd_bitmask_free(link_set);
    }

    if (!exclude_inprocess) {
        //
        // Forward to in-process subscribers
        //
        qdr_subscription_t *sub = DEQ_HEAD(addr->subscriptions);
        while (sub) {
            qdr_forward_on_message_CT(core, sub, in_delivery ? in_delivery->link : 0, msg);
            fanout++;
            addr->deliveries_to_container++;
            sub = DEQ_NEXT(sub);
        }
    }

    if (link_exclusion)
        qd_bitmask_free(link_exclusion);
    return fanout;
}


int qdr_forward_closest_CT(qdr_core_t      *core,
                           qdr_address_t   *addr,
                           qd_message_t    *msg,
                           qdr_delivery_t  *in_delivery,
                           bool             exclude_inprocess,
                           bool             control,
                           qd_bitmask_t    *link_exclusion)
{
    qdr_link_t     *out_link;
    qdr_delivery_t *out_delivery;

    //
    // The Anycast forwarders don't respect link exclusions.
    //
    if (link_exclusion)
        qd_bitmask_free(link_exclusion);

    //
    // Forward to an in-process subscriber if there is one.
    //
    if (!exclude_inprocess) {
        qdr_subscription_t *sub = DEQ_HEAD(addr->subscriptions);
        if (sub) {
            qdr_forward_on_message_CT(core, sub, in_delivery ? in_delivery->link : 0, msg);

            //
            // If the incoming delivery is not settled, it should be accepted and settled here.
            //
            if (in_delivery && !in_delivery->settled) {
                in_delivery->disposition = PN_ACCEPTED;
                in_delivery->settled     = true;
                qdr_delivery_push_CT(core, in_delivery);
            }

            //
            // Rotate this subscription to the end of the list to get round-robin distribution.
            //
            if (DEQ_SIZE(addr->subscriptions) > 1) {
                DEQ_REMOVE_HEAD(addr->subscriptions);
                DEQ_INSERT_TAIL(addr->subscriptions, sub);
            }

            addr->deliveries_to_container++;
            return 1;
        }
    }

    //
    // Forward to a local subscriber.
    //
    qdr_link_ref_t *link_ref = DEQ_HEAD(addr->rlinks);
    if (link_ref) {
        out_link     = link_ref->link;
        out_delivery = qdr_forward_new_delivery_CT(core, in_delivery, out_link, msg);
        qdr_forward_deliver_CT(core, out_link, out_delivery);

        //
        // If there are multiple local subscribers, rotate the list of link references
        // so deliveries will be distributed among the subscribers in a round-robin pattern.
        //
        if (DEQ_SIZE(addr->rlinks) > 1) {
            DEQ_REMOVE_HEAD(addr->rlinks);
            DEQ_INSERT_TAIL(addr->rlinks, link_ref);
        }

        addr->deliveries_egress++;
        return 1;
    }

    //
    // Forward to remote routers with subscribers using the appropriate
    // link for the traffic class: control or data
    //
    // TODO - presently, this picks one remote link to send to.  This needs
    //        to be enhanced so it properly chooses the route to the closest destination.
    //
    int         router_bit;
    qdr_node_t *next_node;

    if (qd_bitmask_first_set(addr->rnodes, &router_bit)) {
        qdr_node_t *rnode = core->routers_by_mask_bit[router_bit];
        if (rnode) {
            if (rnode->next_hop)
                next_node = rnode->next_hop;
            else
                next_node = rnode;

            out_link = control ? next_node->peer_control_link : next_node->peer_data_link;
            if (out_link) {
                out_delivery = qdr_forward_new_delivery_CT(core, in_delivery, out_link, msg);
                qdr_forward_deliver_CT(core, out_link, out_delivery);
                addr->deliveries_transit++;
                return 1;
            }
        }
    }



    return 0;
}


int qdr_forward_balanced_CT(qdr_core_t      *core,
                            qdr_address_t   *addr,
                            qd_message_t    *msg,
                            qdr_delivery_t  *in_delivery,
                            bool             exclude_inprocess,
                            bool             control,
                            qd_bitmask_t    *link_exclusion)
{
    return 0;
}


void qdr_forward_link_balanced_CT(qdr_core_t      *core,
                                  qdr_forwarder_t *forw,
                                  qdr_link_t      *link)
{
}


//==================================================================================
// In-Thread API Functions
//==================================================================================

qdr_forwarder_t *qdr_new_forwarder(qdr_forward_message_t fm, qdr_forward_attach_t fa, bool bypass_valid_origins)
{
    qdr_forwarder_t *forw = NEW(qdr_forwarder_t);

    forw->forward_message      = fm;
    forw->forward_attach       = fa;
    forw->bypass_valid_origins = bypass_valid_origins;

    return forw;
}


void qdr_forwarder_setup_CT(qdr_core_t *core)
{
    //
    // Create message forwarders
    //
    core->forwarders[QD_SEMANTICS_MULTICAST_FLOOD]  = qdr_new_forwarder(qdr_forward_multicast_CT, 0, true);
    core->forwarders[QD_SEMANTICS_MULTICAST_ONCE]   = qdr_new_forwarder(qdr_forward_multicast_CT, 0, false);
    core->forwarders[QD_SEMANTICS_ANYCAST_CLOSEST]  = qdr_new_forwarder(qdr_forward_closest_CT,   0, false);
    core->forwarders[QD_SEMANTICS_ANYCAST_BALANCED] = qdr_new_forwarder(qdr_forward_balanced_CT,  0, false);

    //
    // Create link forwarders
    //
    core->forwarders[QD_SEMANTICS_LINK_BALANCED] = qdr_new_forwarder(0, qdr_forward_link_balanced_CT, false);
}


qdr_forwarder_t *qdr_forwarder_CT(qdr_core_t *core, qd_address_semantics_t semantics)
{
    if (semantics <= QD_SEMANTICS_LINK_BALANCED)
        return core->forwarders[semantics];
    return 0;
}


int qdr_forward_message_CT(qdr_core_t *core, qdr_address_t *addr, qd_message_t *msg, qdr_delivery_t *in_delivery,
                           bool exclude_inprocess, bool control, qd_bitmask_t *link_exclusion)
{
    if (addr->forwarder)
        return addr->forwarder->forward_message(core, addr, msg, in_delivery, exclude_inprocess, control, link_exclusion);

    // TODO - Deal with this delivery's disposition
    return 0;
}


void qdr_forward_attach_CT(qdr_core_t *core, qdr_forwarder_t *forwarder, qdr_link_t *in_link)
{
}

