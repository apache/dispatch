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

#include "test_case.h"
#include <stdio.h>
#include <string.h>
#include "message_private.h"
#include <qpid/dispatch/iterator.h>
#include <qpid/dispatch/amqp.h>
#include <proton/message.h>

static unsigned char buffer[10000];

static size_t flatten_bufs(qd_message_content_t *content)
{
    unsigned char *cursor = buffer;
    qd_buffer_t *buf      = DEQ_HEAD(content->buffers);

    while (buf) {
        memcpy(cursor, qd_buffer_base(buf), qd_buffer_size(buf));
        cursor += qd_buffer_size(buf);
        buf = buf->next;
    }

    return (size_t) (cursor - buffer);
}


static void set_content(qd_message_content_t *content, unsigned char *buffer, size_t len)
{
    unsigned char        *cursor = buffer;
    qd_buffer_t *buf;

    while (len > (size_t) (cursor - buffer)) {
        buf = qd_buffer();
        size_t segment   = qd_buffer_capacity(buf);
        size_t remaining = len - (size_t) (cursor - buffer);
        if (segment > remaining)
            segment = remaining;
        memcpy(qd_buffer_base(buf), cursor, segment);
        cursor += segment;
        qd_buffer_insert(buf, segment);
        DEQ_INSERT_TAIL(content->buffers, buf);
    }
    content->receive_complete = true;
}


static void set_content_bufs(qd_message_content_t *content, int nbufs)
{
    for (; nbufs > 0; nbufs--) {
        qd_buffer_t *buf = qd_buffer();
        size_t segment   = qd_buffer_capacity(buf);
        qd_buffer_insert(buf, segment);
        DEQ_INSERT_TAIL(content->buffers, buf);
    }
}


static char* test_send_to_messenger(void *context)
{
    qd_message_t         *msg     = qd_message();
    qd_message_content_t *content = MSG_CONTENT(msg);
    qd_message_compose_1(msg, "test_addr_0", 0);
    qd_buffer_t *buf = DEQ_HEAD(content->buffers);
    if (buf == 0) {
        qd_message_free(msg);
        return "Expected a buffer in the test message";
    }

    pn_message_t *pn_msg = pn_message();
    size_t len = flatten_bufs(content);
    int result = pn_message_decode(pn_msg, (char *)buffer, len);
    if (result != 0) {
        pn_message_free(pn_msg);
        qd_message_free(msg);
        return "Error in pn_message_decode";
    }

    if (strcmp(pn_message_get_address(pn_msg), "test_addr_0") != 0) {
        pn_message_free(pn_msg);
        qd_message_free(msg);
        return "Address mismatch in received message";
    }

    pn_message_free(pn_msg);
    qd_message_free(msg);

    return 0;
}


static char* test_receive_from_messenger(void *context)
{
    pn_message_t *pn_msg = pn_message();
    pn_message_set_address(pn_msg, "test_addr_1");

    size_t       size = 10000;
    int result = pn_message_encode(pn_msg, (char *)buffer, &size);
    if (result != 0) {
        pn_message_free(pn_msg);
        return "Error in pn_message_encode";
    }

    qd_message_t         *msg     = qd_message();
    qd_message_content_t *content = MSG_CONTENT(msg);

    set_content(content, buffer, size);

    if (qd_message_check_depth(msg, QD_DEPTH_ALL) != QD_MESSAGE_DEPTH_OK) {
        pn_message_free(pn_msg);
        qd_message_free(msg);
        return "qd_message_check_depth returns 'invalid'";
    }

    qd_iterator_t *iter = qd_message_field_iterator(msg, QD_FIELD_TO);
    if (iter == 0) {
        pn_message_free(pn_msg);
        qd_message_free(msg);
        return "Expected an iterator for the 'to' field";
    }

    if (!qd_iterator_equal(iter, (unsigned char*) "test_addr_1")) {
        qd_iterator_free(iter);
        pn_message_free(pn_msg);
        qd_message_free(msg);
        return "Mismatched 'to' field contents";
    }
    qd_iterator_free(iter);

    ssize_t  test_len = (size_t)qd_message_field_length(msg, QD_FIELD_TO);
    if (test_len != 11) {
        pn_message_free(pn_msg);
        qd_message_free(msg);
        return "Incorrect field length";
    }

    char test_field[100];
    size_t hdr_length;
    test_len = qd_message_field_copy(msg, QD_FIELD_TO, test_field, &hdr_length);
    if (test_len - hdr_length != 11) {
        pn_message_free(pn_msg);
        qd_message_free(msg);
        return "Incorrect length returned from field_copy";
    }

    if (test_len < 0) {
        pn_message_free(pn_msg);
        qd_message_free(msg);
        return "test_len cannot be less than zero";
    }
    test_field[test_len] = '\0';
    if (strcmp(test_field + hdr_length, "test_addr_1") != 0) {
        pn_message_free(pn_msg);
        qd_message_free(msg);
        return "Incorrect field content returned from field_copy";
    }

    pn_message_free(pn_msg);
    qd_message_free(msg);

    return 0;
}


// load a few interesting message properties and validate
static char* test_message_properties(void *context)
{
    pn_atom_t id = {.type = PN_STRING,
                    .u.as_bytes.start = "messageId",
                    .u.as_bytes.size = 9};
    pn_atom_t cid = {.type = PN_STRING,
                     .u.as_bytes.start = "correlationId",
                     .u.as_bytes.size = 13};
    const char *subject = "A Subject";
    pn_message_t *pn_msg = pn_message();
    pn_message_set_id(pn_msg, id);
    pn_message_set_subject(pn_msg, subject);
    pn_message_set_correlation_id(pn_msg, cid);

    size_t       size = 10000;
    int result = pn_message_encode(pn_msg, (char *)buffer, &size);
    pn_message_free(pn_msg);

    if (result != 0) return "Error in pn_message_encode";

    qd_message_t         *msg     = qd_message();
    qd_message_content_t *content = MSG_CONTENT(msg);

    set_content(content, buffer, size);

    qd_iterator_t *iter = qd_message_field_iterator(msg, QD_FIELD_CORRELATION_ID);
    if (!iter) {
        qd_message_free(msg);
        return "Expected iterator for the 'correlation-id' field";
    }
    if (qd_iterator_length(iter) != 13) {
        qd_iterator_free(iter);
        qd_message_free(msg);
        return "Bad length for correlation-id";
    }
    if (!qd_iterator_equal(iter, (const unsigned char *)"correlationId")) {
        qd_iterator_free(iter);
        qd_message_free(msg);
        return "Invalid correlation-id";
    }
    qd_iterator_free(iter);

    iter = qd_message_field_iterator(msg, QD_FIELD_SUBJECT);
    if (!iter) {
        qd_iterator_free(iter);
        qd_message_free(msg);
        return "Expected iterator for the 'subject' field";
    }
    if (!qd_iterator_equal(iter, (const unsigned char *)subject)) {
        qd_iterator_free(iter);
        qd_message_free(msg);
        return "Bad value for subject";
    }
    qd_iterator_free(iter);

    iter = qd_message_field_iterator(msg, QD_FIELD_MESSAGE_ID);
    if (!iter) {
        qd_message_free(msg);
        return "Expected iterator for the 'message-id' field";
    }
    if (qd_iterator_length(iter) != 9) {
        qd_iterator_free(iter);
        qd_message_free(msg);
        return "Bad length for message-id";
    }
    if (!qd_iterator_equal(iter, (const unsigned char *)"messageId")) {
        qd_iterator_free(iter);
        qd_message_free(msg);
        return "Invalid message-id";
    }
    qd_iterator_free(iter);

    iter = qd_message_field_iterator(msg, QD_FIELD_TO);
    if (iter) {
        qd_iterator_free(iter);
        qd_message_free(msg);
        return "Expected no iterator for the 'to' field";
    }
    qd_iterator_free(iter);

    qd_message_free(msg);

    return 0;
}


// run qd_message_check_depth against different legal AMQP message
//
static char* _check_all_depths(qd_message_t *msg)
{
    static const qd_message_depth_t depths[] = {
        // yep: purposely out of order
        QD_DEPTH_MESSAGE_ANNOTATIONS,
        QD_DEPTH_DELIVERY_ANNOTATIONS,
        QD_DEPTH_PROPERTIES,
        QD_DEPTH_HEADER,
        QD_DEPTH_APPLICATION_PROPERTIES,
        QD_DEPTH_BODY
    };
    static const int n_depths = 6;

    static char err[1024];

    for (int i = 0; i < n_depths; ++i) {
        if (qd_message_check_depth(msg, depths[i]) != QD_MESSAGE_DEPTH_OK) {
            snprintf(err, 1023,
                     "qd_message_check_depth returned 'invalid' for section 0x%X", (unsigned int)depths[i]);
            err[1023] = 0;
            return err;
        }
    }
    return 0;
}


static char* test_check_multiple(void *context)
{
    // case 1: a minimal encoded message
    //
    pn_message_t *pn_msg = pn_message();

    size_t size = 10000;
    int result = pn_message_encode(pn_msg, (char *)buffer, &size);
    pn_message_free(pn_msg);
    if (result != 0) return "Error in pn_message_encode";

    qd_message_t         *msg     = qd_message();
    qd_message_content_t *content = MSG_CONTENT(msg);

    set_content(content, buffer, size);
    char *rc = _check_all_depths(msg);
    qd_message_free(msg);
    if (rc) return rc;

    // case 2: minimal, with address field in header
    //
    pn_msg = pn_message();
    pn_message_set_address(pn_msg, "test_addr_2");
    size = 10000;
    result = pn_message_encode(pn_msg, (char *)buffer, &size);
    pn_message_free(pn_msg);
    if (result != 0) return "Error in pn_message_encode";
    msg = qd_message();
    set_content(MSG_CONTENT(msg), buffer, size);
    rc = _check_all_depths(msg);
    qd_message_free(msg);
    if (rc) return rc;

    // case 3: null body
    //
    pn_msg = pn_message();
    pn_data_t *body = pn_message_body(pn_msg);
    pn_data_put_null(body);
    size = 10000;
    result = pn_message_encode(pn_msg, (char *)buffer, &size);
    pn_message_free(pn_msg);
    if (result != 0) return "Error in pn_message_encode";
    msg = qd_message();
    set_content(MSG_CONTENT(msg), buffer, size);
    rc = _check_all_depths(msg);
    qd_message_free(msg);
    if (rc) return rc;

    // case 4: minimal legal AMQP 1.0 message (as defined by the standard)
    // A single body field with a null value
    const unsigned char null_body[] = {0x00, 0x53, 0x77, 0x40};
    size = sizeof(null_body);
    memcpy(buffer, null_body, size);
    msg = qd_message();
    set_content(MSG_CONTENT(msg), buffer, size);
    rc = _check_all_depths(msg);
    qd_message_free(msg);
    return rc;
}


static char* test_send_message_annotations(void *context)
{
    qd_message_t         *msg     = qd_message();
    qd_message_content_t *content = MSG_CONTENT(msg);

    qd_composed_field_t *trace = qd_compose_subfield(0);
    qd_compose_start_list(trace);
    qd_compose_insert_string(trace, "Node1");
    qd_compose_insert_string(trace, "Node2");
    qd_compose_end_list(trace);
    qd_message_set_trace_annotation(msg, trace);

    qd_composed_field_t *to_override = qd_compose_subfield(0);
    qd_compose_insert_string(to_override, "to/address");
    qd_message_set_to_override_annotation(msg, to_override);

    qd_composed_field_t *ingress = qd_compose_subfield(0);
    qd_compose_insert_string(ingress, "distress");
    qd_message_set_ingress_annotation(msg, ingress);

    qd_message_compose_1(msg, "test_addr_0", 0);
    qd_buffer_t *buf = DEQ_HEAD(content->buffers);
    if (buf == 0) {
        qd_message_free(msg);
        return "Expected a buffer in the test message";
    }

    pn_message_t *pn_msg = pn_message();
    size_t len = flatten_bufs(content);
    int result = pn_message_decode(pn_msg, (char *)buffer, len);
    if (result != 0) {
        qd_message_free(msg);
        return "Error in pn_message_decode";
    }

    pn_data_t *ma = pn_message_annotations(pn_msg);
    if (!ma) {
        qd_message_free(msg);
        return "Missing message annotations";
    }
    pn_data_rewind(ma);
    pn_data_next(ma);
    if (pn_data_type(ma) != PN_MAP) return "Invalid message annotation type";
    if (pn_data_get_map(ma) != QD_MA_N_KEYS * 2) return "Invalid map length";
    pn_data_enter(ma);
    for (int i = 0; i < QD_MA_N_KEYS; i++) {
        pn_data_next(ma);
        if (pn_data_type(ma) != PN_SYMBOL) return "Bad map index";
        pn_bytes_t sym = pn_data_get_symbol(ma);
        if (!strncmp(QD_MA_PREFIX, sym.start, sym.size)) {
            pn_data_next(ma);
            sym = pn_data_get_string(ma);
        } else if (!strncmp(QD_MA_INGRESS, sym.start, sym.size)) {
            pn_data_next(ma);
            sym = pn_data_get_string(ma);
            if (strncmp("distress", sym.start, sym.size)) return "Bad ingress";
            //fprintf(stderr, "[%.*s]\n", (int)sym.size, sym.start);
        } else if (!strncmp(QD_MA_TO, sym.start, sym.size)) {
            pn_data_next(ma);
            sym = pn_data_get_string(ma);
            if (strncmp("to/address", sym.start, sym.size)) return "Bad to override";
            //fprintf(stderr, "[%.*s]\n", (int)sym.size, sym.start);
        } else if (!strncmp(QD_MA_TRACE, sym.start, sym.size)) {
            pn_data_next(ma);
            if (pn_data_type(ma) != PN_LIST) return "List not found";
            pn_data_enter(ma);
            pn_data_next(ma);
            sym = pn_data_get_string(ma);
            if (strncmp("Node1", sym.start, sym.size)) return "Bad trace entry";
            //fprintf(stderr, "[%.*s]\n", (int)sym.size, sym.start);
            pn_data_next(ma);
            sym = pn_data_get_string(ma);
            if (strncmp("Node2", sym.start, sym.size)) return "Bad trace entry";
            //fprintf(stderr, "[%.*s]\n", (int)sym.size, sym.start);
            pn_data_exit(ma);
        } else return "Unexpected map key";
    }

    pn_message_free(pn_msg);
    qd_message_free(msg);

    return 0;
}


static char* test_q2_input_holdoff_sensing(void *context)
{
    if (QD_QLIMIT_Q2_LOWER >= QD_QLIMIT_Q2_UPPER)
        return "QD_LIMIT_Q2 lower limit is bigger than upper limit";

    for (int nbufs=1; nbufs<QD_QLIMIT_Q2_UPPER + 1; nbufs++) {
        qd_message_t         *msg     = qd_message();
        qd_message_content_t *content = MSG_CONTENT(msg);

        set_content_bufs(content, nbufs);
        if (qd_message_Q2_holdoff_should_block(msg) != (nbufs >= QD_QLIMIT_Q2_UPPER)) {
            qd_message_free(msg);
            return "qd_message_holdoff_would_block was miscalculated";
        }
        if (qd_message_Q2_holdoff_should_unblock(msg) != (nbufs < QD_QLIMIT_Q2_LOWER)) {
            qd_message_free(msg);
            return "qd_message_holdoff_would_unblock was miscalculated";
        }

        qd_message_free(msg);
    }
    return 0;
}


// verify that message check does not incorrectly validate a message section
// that has not been completely received.
//
static char *test_incomplete_annotations(void *context)
{
    const char big_string[] =
        "0123456789012345678901234567890123456789012345678901234567890123456789012345678901234567890123456789"
        "0123456789012345678901234567890123456789012345678901234567890123456789012345678901234567890123456789"
        "0123456789012345678901234567890123456789012345678901234567890123456789012345678901234567890123456789"
        "0123456789012345678901234567890123456789012345678901234567890123456789012345678901234567890123456789"
        "0123456789012345678901234567890123456789012345678901234567890123456789012345678901234567890123456789"
        "0123456789012345678901234567890123456789012345678901234567890123456789012345678901234567890123456789"
        "0123456789012345678901234567890123456789012345678901234567890123456789012345678901234567890123456789"
        "0123456789012345678901234567890123456789012345678901234567890123456789012345678901234567890123456789"
        "0123456789012345678901234567890123456789012345678901234567890123456789012345678901234567890123456789"
        "0123456789012345678901234567890123456789012345678901234567890123456789012345678901234567890123456789";

    char *result = 0;
    qd_message_t *msg = 0;
    pn_message_t *out_message = pn_message();

    pn_data_t *body = pn_message_body(out_message);
    pn_data_clear(body);
    pn_data_put_list(body);
    pn_data_enter(body);
    pn_data_put_long(body, 1);
    pn_data_put_long(body, 2);
    pn_data_put_long(body, 3);
    pn_data_exit(body);

    // Add a bunch 'o user message annotations
    pn_data_t *annos = pn_message_annotations(out_message);
    pn_data_clear(annos);
    pn_data_put_map(annos);
    pn_data_enter(annos);

    pn_data_put_symbol(annos, pn_bytes(strlen("my-key"), "my-key"));
    pn_data_put_string(annos, pn_bytes(strlen("my-data"), "my-data"));

    pn_data_put_symbol(annos, pn_bytes(strlen("my-other-key"), "my-other-key"));
    pn_data_put_string(annos, pn_bytes(strlen("my-other-data"), "my-other-data"));

    // embedded map
    pn_data_put_symbol(annos, pn_bytes(strlen("my-map"), "my-map"));
    pn_data_put_map(annos);
    pn_data_enter(annos);
    pn_data_put_symbol(annos, pn_bytes(strlen("my-map-key1"), "my-map-key1"));
    pn_data_put_char(annos, 'X');
    pn_data_put_symbol(annos, pn_bytes(strlen("my-map-key2"), "my-map-key2"));
    pn_data_put_byte(annos, 0x12);
    pn_data_put_symbol(annos, pn_bytes(strlen("my-map-key3"), "my-map-key3"));
    pn_data_put_string(annos, pn_bytes(strlen("Are We Not Men?"), "Are We Not Men?"));
    pn_data_put_symbol(annos, pn_bytes(strlen("my-last-key"), "my-last-key"));
    pn_data_put_binary(annos, pn_bytes(sizeof(big_string), big_string));
    pn_data_exit(annos);

    pn_data_put_symbol(annos, pn_bytes(strlen("my-ulong"), "my-ulong"));
    pn_data_put_ulong(annos, 0xDEADBEEFCAFEBEEF);

    // embedded list
    pn_data_put_symbol(annos, pn_bytes(strlen("my-list"), "my-list"));
    pn_data_put_list(annos);
    pn_data_enter(annos);
    pn_data_put_string(annos, pn_bytes(sizeof(big_string), big_string));
    pn_data_put_double(annos, 3.1415);
    pn_data_put_short(annos, 1966);
    pn_data_exit(annos);

    pn_data_put_symbol(annos, pn_bytes(strlen("my-bool"), "my-bool"));
    pn_data_put_bool(annos, false);

    pn_data_exit(annos);

    // now encode it

    size_t encode_len = sizeof(buffer);
    int rc = pn_message_encode(out_message, (char *)buffer, &encode_len);
    if (rc) {
        if (rc == PN_OVERFLOW)
            result = "Error: sizeof(buffer) in message_test.c too small - update it!";
        else
            result = "Error encoding message";
        goto exit;
    }

    assert(encode_len > 100);  // you broke the test!

    // Verify that the message check fails unless the entire annotations are
    // present.  First copy in only the first 100 bytes: enough for the MA
    // section header but not the whole section

    msg = qd_message();
    qd_message_content_t *content = MSG_CONTENT(msg);
    set_content(content, buffer, 100);
    content->receive_complete = false;   // more data coming!
    if (qd_message_check_depth(msg, QD_DEPTH_MESSAGE_ANNOTATIONS) != QD_MESSAGE_DEPTH_INCOMPLETE) {
        result = "Error: incomplete message was not detected!";
        goto exit;
    }

    // now complete the message
    set_content(content, &buffer[100], encode_len - 100);
    if (qd_message_check_depth(msg, QD_DEPTH_MESSAGE_ANNOTATIONS) != QD_MESSAGE_DEPTH_OK) {
        result = "Error: expected message to be valid!";
    }

exit:

    if (out_message) pn_message_free(out_message);
    if (msg) qd_message_free(msg);

    return result;
}


static char *test_check_weird_messages(void *context)
{
    char *result = 0;
    qd_message_t *msg = qd_message();

    // case 1:
    // delivery annotations with empty map
    unsigned char da_map[] = {0x00, 0x80,
                              0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x71,
                              0xc1, 0x01, 0x00};
    // first test an incomplete pattern:
    set_content(MSG_CONTENT(msg), da_map, 4);
    MSG_CONTENT(msg)->receive_complete = false;
    qd_message_depth_status_t mc = qd_message_check_depth(msg, QD_DEPTH_DELIVERY_ANNOTATIONS);
    if (mc != QD_MESSAGE_DEPTH_INCOMPLETE) {
        result = "Expected INCOMPLETE status";
        goto exit;
    }

    // full pattern, but no tag
    set_content(MSG_CONTENT(msg), &da_map[4], 6);
    MSG_CONTENT(msg)->receive_complete = false;
    mc = qd_message_check_depth(msg, QD_DEPTH_DELIVERY_ANNOTATIONS);
    if (mc != QD_MESSAGE_DEPTH_INCOMPLETE) {
        result = "Expected INCOMPLETE status";
        goto exit;
    }

    // add tag, but incomplete field:
    set_content(MSG_CONTENT(msg), &da_map[10], 1);
    MSG_CONTENT(msg)->receive_complete = false;
    mc = qd_message_check_depth(msg, QD_DEPTH_DELIVERY_ANNOTATIONS);
    if (mc != QD_MESSAGE_DEPTH_INCOMPLETE) {
        result = "Expected INCOMPLETE status";
        goto exit;
    }

    // and finish up
    set_content(MSG_CONTENT(msg), &da_map[11], 2);
    mc = qd_message_check_depth(msg, QD_DEPTH_DELIVERY_ANNOTATIONS);
    if (mc != QD_MESSAGE_DEPTH_OK) {
        result = "Expected OK status";
        goto exit;
    }

    // case 2: negative test - detect invalid tag
    unsigned char bad_hdr[] = {0x00, 0x53, 0x70, 0xC1};  // 0xc1 == map, not list!
    qd_message_free(msg);
    msg = qd_message();
    set_content(MSG_CONTENT(msg), bad_hdr, sizeof(bad_hdr));
    MSG_CONTENT(msg)->receive_complete = false;
    mc = qd_message_check_depth(msg, QD_DEPTH_DELIVERY_ANNOTATIONS); // looking _past_ header!
    if (mc != QD_MESSAGE_DEPTH_INVALID) {
        result = "Bad tag not detected!";
        goto exit;
    }

    // case 3: check the valid body types
    unsigned char body_bin[] = {0x00, 0x80, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x75,
                                0xA0, 0x03, 0x00, 0x01, 0x02};
    qd_message_free(msg);
    msg = qd_message();
    set_content(MSG_CONTENT(msg), body_bin, sizeof(body_bin));
    mc = qd_message_check_depth(msg, QD_DEPTH_ALL); // looking _past_ header!
    if (mc != QD_MESSAGE_DEPTH_OK) {
        result = "Expected OK bin body";
        goto exit;
    }

    unsigned char body_seq[] = {0x00, 0x53, 0x76, 0x45};
    qd_message_free(msg);
    msg = qd_message();
    set_content(MSG_CONTENT(msg), body_seq, sizeof(body_seq));
    mc = qd_message_check_depth(msg, QD_DEPTH_BODY);
    if (mc != QD_MESSAGE_DEPTH_OK) {
        result = "Expected OK seq body";
        goto exit;
    }

    unsigned char body_value[] = {0x00, 0x53, 0x77, 0x51, 0x99};
    qd_message_free(msg);
    msg = qd_message();
    set_content(MSG_CONTENT(msg), body_value, sizeof(body_value));
    mc = qd_message_check_depth(msg, QD_DEPTH_BODY);
    if (mc != QD_MESSAGE_DEPTH_OK) {
        result = "Expected OK value body";
        goto exit;
    }

exit:
    if (msg) qd_message_free(msg);
    return result;
}


int message_tests(void)
{
    int result = 0;
    char *test_group = "message_tests";

    TEST_CASE(test_send_to_messenger, 0);
    TEST_CASE(test_receive_from_messenger, 0);
    TEST_CASE(test_message_properties, 0);
    TEST_CASE(test_check_multiple, 0);
    TEST_CASE(test_send_message_annotations, 0);
    TEST_CASE(test_q2_input_holdoff_sensing, 0);
    TEST_CASE(test_incomplete_annotations, 0);
    TEST_CASE(test_check_weird_messages, 0);

    return result;
}

