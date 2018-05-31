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

#include "qpid/dispatch/python_embedded.h"
#include "policy.h"
#include "policy_internal.h"
#include "parse_tree.h"
#include <stdio.h>
#include <string.h>
#include "dispatch_private.h"
#include "qpid/dispatch/container.h"
#include "qpid/dispatch/server.h"
#include <proton/message.h>
#include <proton/condition.h>
#include <proton/connection.h>
#include <proton/transport.h>
#include <proton/error.h>
#include <proton/event.h>
#include "python_private.h"


//
// The current statistics maintained globally through multiple
// reconfiguration of policy settings.
//
static int n_connections = 0;
static int n_denied = 0;
static int n_processed = 0;

//
// error conditions signaled to effect denial
//

//
// error descriptions signaled to effect denial
//
static char* CONNECTION_DISALLOWED         = "connection disallowed by local policy";
static char* SESSION_DISALLOWED            = "session disallowed by local policy";
static char* LINK_DISALLOWED               = "link disallowed by local policy";

//
// Policy configuration/statistics management interface
//
struct qd_policy_t {
    qd_dispatch_t        *qd;
    qd_log_source_t      *log_source;
    void                 *py_policy_manager;
    sys_mutex_t          *tree_lock;
    qd_parse_tree_t      *hostname_tree;
                          // configured settings
    int                   max_connection_limit;
    char                 *policyDir;
    bool                  enableVhostPolicy;
    bool                  enableVhostNamePatterns;
                          // live statistics
    int                   connections_processed;
    int                   connections_denied;
    int                   connections_current;
};

/** Create the policy structure
 * @param[in] qd pointer the the qd
 **/
qd_policy_t *qd_policy(qd_dispatch_t *qd)
{
    qd_policy_t *policy = NEW(qd_policy_t);
    ZERO(policy);
    policy->qd                   = qd;
    policy->log_source           = qd_log_source("POLICY");
    policy->max_connection_limit = 65535;
    policy->tree_lock            = sys_mutex();
    policy->hostname_tree        = qd_parse_tree_new(QD_PARSE_TREE_ADDRESS);

    qd_log(policy->log_source, QD_LOG_TRACE, "Policy Initialized");
    return policy;
}


/** Free the policy structure
 * @param[in] policy pointer to the policy
 **/
void qd_policy_free(qd_policy_t *policy)
{
    if (policy->policyDir)
        free(policy->policyDir);
    qd_parse_tree_free(policy->hostname_tree);
    free(policy);
}

//
//
#define CHECK() if (qd_error_code()) goto error

qd_error_t qd_entity_configure_policy(qd_policy_t *policy, qd_entity_t *entity)
{
    policy->max_connection_limit = qd_entity_opt_long(entity, "maxConnections", 65535); CHECK();
    if (policy->max_connection_limit < 0)
        return qd_error(QD_ERROR_CONFIG, "maxConnections must be >= 0");
    policy->policyDir =
        qd_entity_opt_string(entity, "policyDir", 0); CHECK();
    policy->enableVhostPolicy = qd_entity_opt_bool(entity, "enableVhostPolicy", false); CHECK();
    policy->enableVhostNamePatterns = qd_entity_opt_bool(entity, "enableVhostNamePatterns", false); CHECK();
    qd_log(policy->log_source, QD_LOG_INFO,
           "Policy configured maxConnections: %d, "
           "policyDir: '%s',"
           "access rules enabled: '%s', "
           "use hostname patterns: '%s'",
           policy->max_connection_limit,
           policy->policyDir,
           (policy->enableVhostPolicy ? "true" : "false"),
           (policy->enableVhostNamePatterns ? "true" : "false"));
    return QD_ERROR_NONE;

error:
    if (policy->policyDir)
        free(policy->policyDir);
    qd_policy_free(policy);
    return qd_error_code();
}


//
//
qd_error_t qd_register_policy_manager(qd_policy_t *policy, void *policy_manager)
{
    policy->py_policy_manager = policy_manager;
    return QD_ERROR_NONE;
}


long qd_policy_c_counts_alloc()
{
    qd_policy_denial_counts_t * dc = NEW(qd_policy_denial_counts_t);
    assert(dc);
    ZERO(dc);
    return (long)dc;
}


void qd_policy_c_counts_free(long ccounts)
{
    void *dc = (void *)ccounts;
    assert(dc);
    free(dc);
}


qd_error_t qd_policy_c_counts_refresh(long ccounts, qd_entity_t *entity)
{
    qd_policy_denial_counts_t *dc = (qd_policy_denial_counts_t*)ccounts;
    if (!qd_entity_set_long(entity, "sessionDenied", dc->sessionDenied) &&
        !qd_entity_set_long(entity, "senderDenied", dc->senderDenied) &&
        !qd_entity_set_long(entity, "receiverDenied", dc->receiverDenied)
    )
        return QD_ERROR_NONE;
    return qd_error_code();
}


/** Update the statistics in qdrouterd.conf["policy"]
 * @param[in] entity pointer to the policy management object
 **/
qd_error_t qd_entity_refresh_policy(qd_entity_t* entity, void *unused) {
    // Return global stats
    if (!qd_entity_set_long(entity, "connectionsProcessed", n_processed) &&
        !qd_entity_set_long(entity, "connectionsDenied", n_denied) &&
        !qd_entity_set_long(entity, "connectionsCurrent", n_connections)
    )
        return QD_ERROR_NONE;
    return qd_error_code();
}


//
// Functions related to absolute connection counts.
// These handle connections at the socket level with
// no regard to user identity. Simple yes/no decisions
// are made and there is no AMQP channel for returning
// error conditions.
//

bool qd_policy_socket_accept(qd_policy_t *policy, const char *hostname)
{
    bool result = true;
    if (n_connections < policy->max_connection_limit) {
        // connection counted and allowed
        n_connections += 1;
        qd_log(policy->log_source, QD_LOG_TRACE, "ALLOW Connection '%s' based on global connection count. nConnections= %d", hostname, n_connections);
    } else {
        // connection denied
        result = false;
        n_denied += 1;
        qd_log(policy->log_source, QD_LOG_INFO, "DENY Connection '%s' based on global connection count. nConnections= %d", hostname, n_connections);
    }
    n_processed += 1;
    return result;
}


//
//
void qd_policy_socket_close(qd_policy_t *policy, const qd_connection_t *conn)
{
    n_connections -= 1;
    assert (n_connections >= 0);
    if (policy->enableVhostPolicy) {
        // HACK ALERT: TODO: This should be deferred to a Python thread
        qd_python_lock_state_t lock_state = qd_python_lock();
        PyObject *module = PyImport_ImportModule("qpid_dispatch_internal.policy.policy_manager");
        if (module) {
            PyObject *close_connection = PyObject_GetAttrString(module, "policy_close_connection");
            if (close_connection) {
                PyObject *result = PyObject_CallFunction(close_connection, "(OK)",
                                                         (PyObject *)policy->py_policy_manager,
                                                          conn->connection_id);
                if (result) {
                    Py_XDECREF(result);
                } else {
                    qd_log(policy->log_source, QD_LOG_DEBUG, "Internal: Connection close failed: result");
                }
                Py_XDECREF(close_connection);
            } else {
                qd_log(policy->log_source, QD_LOG_DEBUG, "Internal: Connection close failed: close_connection");
            }
            Py_XDECREF(module);
        } else {
            qd_log(policy->log_source, QD_LOG_DEBUG, "Internal: Connection close failed: module");
        }
        qd_python_unlock(lock_state);
    }
    const char *hostname = qd_connection_name(conn);
    qd_log(policy->log_source, QD_LOG_DEBUG, "Connection '%s' closed with resources n_sessions=%d, n_senders=%d, n_receivers=%d. nConnections= %d.",
            hostname, conn->n_sessions, conn->n_senders, conn->n_receivers, n_connections);
}


// C in the CSV string
static const char* QPALN_COMMA_SEP =",";

//
// Given a CSV string defining parser tree specs for allowed sender or
// receiver links, return a parse_tree
//
//  @param config_spec CSV string with link name match patterns
//  @return pointer to parse tree
//
qd_parse_tree_t * qd_policy_parse_tree(const char *config_spec)
{
    if (!config_spec || strlen(config_spec) == 0)
        // empty config specs never match so don't even create parse tree
        return NULL;

    qd_parse_tree_t *tree = qd_parse_tree_new(QD_PARSE_TREE_ADDRESS);
    if (!tree)
        return NULL;

    // Add CSV's values to the tree.
    // Note that tree's pattern is unused. This code uses a dummy '1'.
    char * dup = strdup(config_spec);
    char * dupend = dup + strlen(dup);
    char * pch = dup;
    while (pch < dupend) {
        size_t vsize = strcspn(pch, QPALN_COMMA_SEP);
        if (vsize > 0) {
            pch[vsize] = '\0';
            qd_parse_tree_add_pattern_str(tree, pch, (void *)1);
        }
        pch += vsize + 1;
    }
    free(dup);
    return tree;
}


//
// Functions related to authenticated connection denial.
// An AMQP Open has been received over some connection.
// Evaluate the connection auth and the Open fields to
// allow or deny the Open. Denied Open attempts are
// effected by returning Open and then Close_with_condition.
//
/** Look up user/host/vhost in python vhost and give the AMQP Open
 *  a go-no_go decision. Return false if the mechanics of calling python
 *  fails. A policy lookup will deny the connection by returning a blank
 *  usergroup name in the name buffer.
 *  Connection and connection denial counting is done in the python code.
 * @param[in] policy pointer to policy
 * @param[in] username authenticated user name
 * @param[in] hostip numeric host ip address
 * @param[in] vhost application name received in remote AMQP Open.hostname
 * @param[in] conn_name connection name for tracking
 * @param[out] name_buf pointer to settings name buffer
 * @param[in] name_buf_size size of settings_buf
 **/
bool qd_policy_open_lookup_user(
    qd_policy_t *policy,
    const char *username,
    const char *hostip,
    const char *vhost,
    const char *conn_name,
    char       *name_buf,
    int         name_buf_size,
    uint64_t    conn_id,
    qd_policy_settings_t *settings)
{
    // Lookup the user/host/vhost for allow/deny and to get settings name
    bool res = false;
    name_buf[0] = 0;
    qd_python_lock_state_t lock_state = qd_python_lock();
    PyObject *module = PyImport_ImportModule("qpid_dispatch_internal.policy.policy_manager");
    if (module) {
        PyObject *lookup_user = PyObject_GetAttrString(module, "policy_lookup_user");
        if (lookup_user) {
            PyObject *result = PyObject_CallFunction(lookup_user, "(OssssK)",
                                                     (PyObject *)policy->py_policy_manager,
                                                     username, hostip, vhost, conn_name, conn_id);
            if (result) {
                char *res_string = py_obj_2_c_string(result);
                const size_t res_len = res_string ? strlen(res_string) : 0;
                if (res_len < name_buf_size) {
                    strcpy(name_buf, res_string);
                } else {
                    qd_log(policy->log_source, QD_LOG_ERROR,
                           "Internal: lookup_user: insufficient buffer for name");
                }
                Py_XDECREF(result);
                free(res_string);
                res = !!name_buf[0]; // settings name returned
            } else {
                qd_log(policy->log_source, QD_LOG_DEBUG, "Internal: lookup_user: result");
            }
            Py_XDECREF(lookup_user);
        } else {
            qd_log(policy->log_source, QD_LOG_DEBUG, "Internal: lookup_user: lookup_user");
        }
    }
    if (!res) {
        if (module) {
            Py_XDECREF(module);
        }
        qd_python_unlock(lock_state);
        return false;
    }

    // 
    if (name_buf[0]) {
        // Go get the named settings
        res = false;
        PyObject *upolicy = PyDict_New();
        if (upolicy) {
            PyObject *lookup_settings = PyObject_GetAttrString(module, "policy_lookup_settings");
            if (lookup_settings) {
                PyObject *result2 = PyObject_CallFunction(lookup_settings, "(OssO)",
                                                        (PyObject *)policy->py_policy_manager,
                                                        vhost, name_buf, upolicy);
                if (result2) {
                    settings->maxFrameSize         = qd_entity_opt_long((qd_entity_t*)upolicy, "maxFrameSize", 0);
                    settings->maxSessionWindow     = qd_entity_opt_long((qd_entity_t*)upolicy, "maxSessionWindow", 0);
                    settings->maxSessions          = qd_entity_opt_long((qd_entity_t*)upolicy, "maxSessions", 0);
                    settings->maxSenders           = qd_entity_opt_long((qd_entity_t*)upolicy, "maxSenders", 0);
                    settings->maxReceivers         = qd_entity_opt_long((qd_entity_t*)upolicy, "maxReceivers", 0);
                    settings->allowAnonymousSender = qd_entity_opt_bool((qd_entity_t*)upolicy, "allowAnonymousSender", false);
                    settings->allowDynamicSource   = qd_entity_opt_bool((qd_entity_t*)upolicy, "allowDynamicSource", false);
                    settings->allowUserIdProxy     = qd_entity_opt_bool((qd_entity_t*)upolicy, "allowUserIdProxy", false);
                    settings->sources              = qd_entity_get_string((qd_entity_t*)upolicy, "sources");
                    settings->targets              = qd_entity_get_string((qd_entity_t*)upolicy, "targets");
                    settings->sourcePattern        = qd_entity_get_string((qd_entity_t*)upolicy, "sourcePattern");
                    settings->targetPattern        = qd_entity_get_string((qd_entity_t*)upolicy, "targetPattern");
                    settings->sourceParseTree      = qd_policy_parse_tree(settings->sourcePattern);
                    settings->targetParseTree      = qd_policy_parse_tree(settings->targetPattern);
                    settings->denialCounts         = (qd_policy_denial_counts_t*)
                                                    qd_entity_get_long((qd_entity_t*)upolicy, "denialCounts");
                    Py_XDECREF(result2);
                    res = true; // named settings content returned
                } else {
                    qd_log(policy->log_source, QD_LOG_DEBUG, "Internal: lookup_user: result2");
                }
                Py_XDECREF(lookup_settings);
            } else {
                qd_log(policy->log_source, QD_LOG_DEBUG, "Internal: lookup_user: lookup_settings");
            }
            Py_XDECREF(upolicy);
        } else {
            qd_log(policy->log_source, QD_LOG_DEBUG, "Internal: lookup_user: upolicy");
        }
    }
    Py_XDECREF(module);
    qd_python_unlock(lock_state);

    if (name_buf[0]) {
        qd_log(policy->log_source,
           QD_LOG_TRACE,
           "ALLOW AMQP Open lookup_user: %s, rhost: %s, vhost: %s, connection: %s. Usergroup: '%s'%s",
           username, hostip, vhost, conn_name, name_buf, (res ? "" : " Internal error."));
    } else {
        // Denials are logged in python code
    }

    return res;
}


//
//
void qd_policy_private_deny_amqp_connection(pn_connection_t *conn, const char *cond_name, const char *cond_descr)
{
    pn_condition_t * cond = pn_connection_condition(conn);
    (void) pn_condition_set_name(       cond, cond_name);
    (void) pn_condition_set_description(cond, cond_descr);
    pn_connection_close(conn);
    // Connection denial counts are counted and logged by python code.
}


//
//
void qd_policy_deny_amqp_session(pn_session_t *ssn, qd_connection_t *qd_conn)
{
    pn_condition_t * cond = pn_session_condition(ssn);
    (void) pn_condition_set_name(       cond, QD_AMQP_COND_RESOURCE_LIMIT_EXCEEDED);
    (void) pn_condition_set_description(cond, SESSION_DISALLOWED);
    pn_session_close(ssn);
    qd_conn->policy_settings->denialCounts->sessionDenied++;
}


//
//
bool qd_policy_approve_amqp_session(pn_session_t *ssn, qd_connection_t *qd_conn)
{
    bool result = true;
    if (qd_conn->policy_settings) {
        if (qd_conn->policy_settings->maxSessions) {
            if (qd_conn->n_sessions == qd_conn->policy_settings->maxSessions) {
                qd_policy_deny_amqp_session(ssn, qd_conn);
                result = false;
            }
        }
    }
    pn_connection_t *conn = qd_connection_pn(qd_conn);
    qd_dispatch_t *qd = qd_server_dispatch(qd_conn->server);
    qd_policy_t *policy = qd->policy;
    const char *hostip = qd_connection_remote_ip(qd_conn);
    const char *vhost = pn_connection_remote_hostname(conn);
    if (result) {
        qd_log(policy->log_source,
           QD_LOG_TRACE,
           "ALLOW AMQP Begin Session. user: %s, rhost: %s, vhost: %s",
           qd_conn->user_id, hostip, vhost);
    } else {
        qd_log(policy->log_source,
           QD_LOG_INFO,
           "DENY AMQP Begin Session due to session limit. user: %s, rhost: %s, vhost: %s",
           qd_conn->user_id, hostip, vhost);
    }
    return result;
}


//
//
void qd_policy_apply_session_settings(pn_session_t *ssn, qd_connection_t *qd_conn)
{
    size_t capacity;
    if (qd_conn->policy_settings && qd_conn->policy_settings->maxSessionWindow) {
        capacity = qd_conn->policy_settings->maxSessionWindow;
    } else {
        const qd_server_config_t * cf = qd_connection_config(qd_conn);
        capacity = cf->incoming_capacity;
    }
    pn_session_set_incoming_capacity(ssn, capacity);
}

//
//
void _qd_policy_deny_amqp_link(pn_link_t *link, qd_connection_t *qd_conn, const char *condition)
{
    pn_condition_t * cond = pn_link_condition(link);
    (void) pn_condition_set_name(       cond, condition);
    (void) pn_condition_set_description(cond, LINK_DISALLOWED);
    pn_link_close(link);
}


//
//
void _qd_policy_deny_amqp_sender_link(pn_link_t *pn_link, qd_connection_t *qd_conn, const char *condition)
{
    _qd_policy_deny_amqp_link(pn_link, qd_conn, condition);
    qd_conn->policy_settings->denialCounts->senderDenied++;
}


//
//
void _qd_policy_deny_amqp_receiver_link(pn_link_t *pn_link, qd_connection_t *qd_conn, const char *condition)
{
    _qd_policy_deny_amqp_link(pn_link, qd_conn, condition);
    qd_conn->policy_settings->denialCounts->receiverDenied++;
}


//
//
#define MIN(a,b) (((a)<(b))?(a):(b))

// substitute "${user}" in place of uname in proposed
char * _qd_policy_link_user_name_subst(const char *uname, const char *proposed, char *obuf, int osize)
{
    if (strlen(uname) == 0)
        return NULL;

    const char duser[] = "${user}";
    char *retptr = obuf;
    const char *wiptr = proposed;
    const char *findptr = strstr(proposed, uname);
    if (findptr == NULL) {
        return NULL;
    }

    // Copy leading before match
    int segsize = findptr - wiptr;
    int copysize = MIN(osize, segsize);
    if (copysize)
        strncpy(obuf, wiptr, copysize);
    wiptr += copysize;
    osize -= copysize;
    obuf  += copysize;

    // Copy the substitution string
    segsize = sizeof(duser) - 1;
    copysize = MIN(osize, segsize);
    if (copysize)
        strncpy(obuf, duser, copysize);
    wiptr += strlen(uname);
    osize -= copysize;
    obuf  += copysize;

    // Copy trailing after match
    strncpy(obuf, wiptr, osize);
    return retptr;
}


//
//
// Size of 'easy' temporary copy of allowed input string
#define QPALN_SIZE 1024
// Size of user-name-substituted proposed string.
#define QPALN_USERBUFSIZE 300
// Wildcard character
#define QPALN_WILDCARD '*'

bool _qd_policy_approve_link_name(const char *username, const char *allowed, const char *proposed)
{
    // Verify string sizes are usable
    size_t p_len = strlen(proposed);
    if (p_len == 0) {
        // degenerate case of blank name being opened. will never match anything.
        return false;
    }
    size_t a_len = strlen(allowed);
    if (a_len == 0) {
        // no names in 'allowed'.
        return false;
    }

    // Create a temporary writable copy of incoming allowed list
    char t_allow[QPALN_SIZE + 1]; // temporary buffer for normal allow lists
    int buflen = sizeof(t_allow);
    char * pa = t_allow;
    if (a_len >= buflen) {
        buflen = a_len + 1;
        pa = (char *)malloc(buflen); // malloc a buffer for larger allow lists
        if (!pa)
            return false;
    }
    strcpy(pa, allowed);        /* We know we have allocated enoough space */
    pa[a_len] = 0;
    // Do reverse user substitution into proposed
    char substbuf[QPALN_USERBUFSIZE];
    char * prop2 = _qd_policy_link_user_name_subst(username, proposed, substbuf, QPALN_USERBUFSIZE);
    char *toknext = 0;
    char *tok = strtok_r(pa, QPALN_COMMA_SEP, &toknext);
    assert (tok);
    bool result = false;
    while (tok != NULL) {
        if (*tok == QPALN_WILDCARD) {
            result = true;
            break;
        }
        int matchlen = p_len;
        int len = strlen(tok);
        if (tok[len-1] == QPALN_WILDCARD) {
            matchlen = len - 1;
            assert(len > 0);
        }
        if (strncmp(tok, proposed, matchlen) == 0) {
            result = true;
            break;
        }
        if (prop2 && strncmp(tok, prop2, matchlen) == 0) {
            result = true;
            break;
        }
        tok = strtok_r(NULL, QPALN_COMMA_SEP, &toknext);
    }
    if (pa != t_allow) {
        free(pa);
    }
    return result;
}


bool _qd_policy_approve_link_name_tree(const char *username, qd_parse_tree_t *tree, const char *proposed)
{
    // Verify string sizes are usable
    size_t p_len = strlen(proposed);
    if (p_len == 0) {
        // degenerate case of blank name being opened. will never match anything.
        return false;
    }
    void * unused_payload = 0;

    if (qd_parse_tree_retrieve_match_str(tree, proposed, &unused_payload))
        return true;

    // Do reverse user substitution into proposed
    char substbuf[QPALN_USERBUFSIZE];
    char * prop2 = _qd_policy_link_user_name_subst(username, proposed, substbuf, QPALN_USERBUFSIZE);
    if (prop2 && qd_parse_tree_retrieve_match_str(tree, prop2, &unused_payload))
        return true;
    return false;
}


bool qd_policy_approve_amqp_sender_link(pn_link_t *pn_link, qd_connection_t *qd_conn)
{
    const char *hostip = qd_connection_remote_ip(qd_conn);
    const char *vhost = pn_connection_remote_hostname(qd_connection_pn(qd_conn));

    if (qd_conn->policy_settings->maxSenders) {
        if (qd_conn->n_senders == qd_conn->policy_settings->maxSenders) {
            // Max sender limit specified and violated.
            qd_log(qd_server_dispatch(qd_conn->server)->policy->log_source, QD_LOG_INFO,
                "DENY AMQP Attach sender for user '%s', rhost '%s', vhost '%s' based on maxSenders limit",
                qd_conn->user_id, hostip, vhost);
            _qd_policy_deny_amqp_sender_link(pn_link, qd_conn, QD_AMQP_COND_RESOURCE_LIMIT_EXCEEDED);
            return false;
        } else {
            // max sender limit not violated
        }
    } else {
        // max sender limit not specified
    }
    // Approve sender link based on target
    const char * target = pn_terminus_get_address(pn_link_remote_target(pn_link));
    bool lookup;
    if (target && *target) {
        // a target is specified
        lookup = qd_policy_approve_link_name(qd_conn->user_id, qd_conn->policy_settings, target, false);

        qd_log(qd_server_dispatch(qd_conn->server)->policy->log_source, (lookup ? QD_LOG_TRACE : QD_LOG_INFO),
            "%s AMQP Attach sender link '%s' for user '%s', rhost '%s', vhost '%s' based on link target name",
            (lookup ? "ALLOW" : "DENY"), target, qd_conn->user_id, hostip, vhost);

        if (!lookup) {
            _qd_policy_deny_amqp_sender_link(pn_link, qd_conn, QD_AMQP_COND_UNAUTHORIZED_ACCESS);
            return false;
        }
    } else {
        // A sender with no remote target.
        // This happens all the time with anonymous relay
        lookup = qd_conn->policy_settings->allowAnonymousSender;
        qd_log(qd_server_dispatch(qd_conn->server)->policy->log_source, (lookup ? QD_LOG_TRACE : QD_LOG_INFO),
            "%s AMQP Attach anonymous sender for user '%s', rhost '%s', vhost '%s'",
            (lookup ? "ALLOW" : "DENY"), qd_conn->user_id, hostip, vhost);
        if (!lookup) {
            _qd_policy_deny_amqp_sender_link(pn_link, qd_conn, QD_AMQP_COND_UNAUTHORIZED_ACCESS);
            return false;
        }
    }
    // Approved
    return true;
}


bool qd_policy_approve_amqp_receiver_link(pn_link_t *pn_link, qd_connection_t *qd_conn)
{
    const char *hostip = qd_connection_remote_ip(qd_conn);
    const char *vhost = pn_connection_remote_hostname(qd_connection_pn(qd_conn));

    if (qd_conn->policy_settings->maxReceivers) {
        if (qd_conn->n_receivers == qd_conn->policy_settings->maxReceivers) {
            // Max sender limit specified and violated.
            qd_log(qd_server_dispatch(qd_conn->server)->policy->log_source, QD_LOG_INFO,
                "DENY AMQP Attach receiver for user '%s', rhost '%s', vhost '%s' based on maxReceivers limit",
                qd_conn->user_id, hostip, vhost);
            _qd_policy_deny_amqp_receiver_link(pn_link, qd_conn, QD_AMQP_COND_RESOURCE_LIMIT_EXCEEDED);
            return false;
        } else {
            // max receiver limit not violated
        }
    } else {
        // max receiver limit not specified
    }
    // Approve receiver link based on source
    bool dynamic_src = pn_terminus_is_dynamic(pn_link_remote_source(pn_link));
    if (dynamic_src) {
        bool lookup = qd_conn->policy_settings->allowDynamicSource;
        qd_log(qd_server_dispatch(qd_conn->server)->policy->log_source, (lookup ? QD_LOG_TRACE : QD_LOG_INFO),
            "%s AMQP Attach receiver dynamic source for user '%s', rhost '%s', vhost '%s',",
            (lookup ? "ALLOW" : "DENY"), qd_conn->user_id, hostip, vhost);
        // Dynamic source policy rendered the decision
        if (!lookup) {
            _qd_policy_deny_amqp_receiver_link(pn_link, qd_conn, QD_AMQP_COND_UNAUTHORIZED_ACCESS);
        }
        return lookup;
    }
    const char * source = pn_terminus_get_address(pn_link_remote_source(pn_link));
    if (source && *source) {
        // a source is specified
        bool lookup = qd_policy_approve_link_name(qd_conn->user_id, qd_conn->policy_settings, source, true);

        qd_log(qd_server_dispatch(qd_conn->server)->policy->log_source, (lookup ? QD_LOG_TRACE : QD_LOG_INFO),
            "%s AMQP Attach receiver link '%s' for user '%s', rhost '%s', vhost '%s' based on link source name",
            (lookup ? "ALLOW" : "DENY"), source, qd_conn->user_id, hostip, vhost);

        if (!lookup) {
            _qd_policy_deny_amqp_receiver_link(pn_link, qd_conn, QD_AMQP_COND_UNAUTHORIZED_ACCESS);
            return false;
        }
    } else {
        // A receiver with no remote source.
        qd_log(qd_server_dispatch(qd_conn->server)->policy->log_source, QD_LOG_INFO,
               "DENY AMQP Attach receiver link '' for user '%s', rhost '%s', vhost '%s'",
               qd_conn->user_id, hostip, vhost);
        _qd_policy_deny_amqp_receiver_link(pn_link, qd_conn, QD_AMQP_COND_UNAUTHORIZED_ACCESS);
        return false;
    }
    // Approved
    return true;
}


void qd_policy_amqp_open(qd_connection_t *qd_conn) {
    pn_connection_t *conn = qd_connection_pn(qd_conn);
    qd_dispatch_t *qd = qd_server_dispatch(qd_conn->server);
    qd_policy_t *policy = qd->policy;
    bool connection_allowed = true;

    if (policy->enableVhostPolicy && (!qd_conn->role || strcmp(qd_conn->role, "inter-router"))) {
        // Open connection or not based on policy.
        pn_transport_t *pn_trans = pn_connection_transport(conn);
        const char *hostip = qd_connection_remote_ip(qd_conn);
        const char *pcrh = pn_connection_remote_hostname(conn);
        const char *vhost = (pcrh ? pcrh : "");
        const char *conn_name = qd_connection_name(qd_conn);
#define SETTINGS_NAME_SIZE 256
        char settings_name[SETTINGS_NAME_SIZE];
        uint32_t conn_id = qd_conn->connection_id;
        if (!qd_conn->policy_settings) {
            qd_conn->policy_settings = NEW(qd_policy_settings_t); // TODO: memory pool for settings
            ZERO(qd_conn->policy_settings);
        }

        if (qd_policy_open_lookup_user(policy, qd_conn->user_id, hostip, vhost, conn_name,
                                       settings_name, SETTINGS_NAME_SIZE, conn_id,
                                       qd_conn->policy_settings) &&
            settings_name[0]) {
            // This connection is allowed by policy.
            // Apply transport policy settings
            if (qd_conn->policy_settings->maxFrameSize > 0)
                pn_transport_set_max_frame(pn_trans, qd_conn->policy_settings->maxFrameSize);
            if (qd_conn->policy_settings->maxSessions > 0)
                pn_transport_set_channel_max(pn_trans, qd_conn->policy_settings->maxSessions - 1);
        } else {
            // This connection is denied by policy.
            connection_allowed = false;
        }
    } else {
        // No policy implies automatic policy allow
        // Note that connections not governed by policy have no policy_settings.
    }
    if (connection_allowed) {
        if (pn_connection_state(conn) & PN_LOCAL_UNINIT)
            pn_connection_open(conn);
        policy_notify_opened(qd_conn->open_container, qd_conn, qd_conn->context);
    } else {
        qd_policy_private_deny_amqp_connection(conn, QD_AMQP_COND_RESOURCE_LIMIT_EXCEEDED, CONNECTION_DISALLOWED);
    }
}


void qd_policy_settings_free(qd_policy_settings_t *settings)
{
    if (!settings) return;
    if (settings->sources)         free(settings->sources);
    if (settings->targets)         free(settings->targets);
    if (settings->sourcePattern)   free(settings->sourcePattern);
    if (settings->targetPattern)   free(settings->targetPattern);
    if (settings->sourceParseTree) qd_parse_tree_free(settings->sourceParseTree);
    if (settings->targetParseTree) qd_parse_tree_free(settings->targetParseTree);
    free (settings);
}


bool qd_policy_approve_link_name(const char *username,
                                 const qd_policy_settings_t *settings,
                                 const char *proposed,
                                 bool isReceiver)
{
    if (isReceiver) {
        if (settings->sourceParseTree) {
            return _qd_policy_approve_link_name_tree(username, settings->sourceParseTree, proposed);
        } else if (settings->sources) {
            return _qd_policy_approve_link_name(username, settings->sources, proposed);
        }
    } else {
        if (settings->targetParseTree) {
            return _qd_policy_approve_link_name_tree(username, settings->targetParseTree, proposed);
        } else if (settings->targets) {
            return _qd_policy_approve_link_name(username, settings->targets, proposed);
        }
    }
    return false;
}


// Add a hostname to the lookup parse_tree
bool qd_policy_host_pattern_add(qd_policy_t *policy, char *hostPattern)
{
    sys_mutex_lock(policy->tree_lock);
    void *oldp = qd_parse_tree_add_pattern_str(policy->hostname_tree, hostPattern, hostPattern);
    if (oldp) {
        void *recovered = qd_parse_tree_add_pattern_str(policy->hostname_tree, (char *)oldp, oldp);
        assert (recovered);
        (void)recovered;        /* Silence compiler complaints of unused variable */
    }
    sys_mutex_unlock(policy->tree_lock);
    if (oldp)
        qd_log(policy->log_source,
            QD_LOG_WARNING,
            "vhost hostname pattern '%s' failed to replace optimized pattern '%s'",
            hostPattern, oldp);
    return oldp == 0;
}


// Remove a hostname from the lookup parse_tree
void qd_policy_host_pattern_remove(qd_policy_t *policy, char *hostPattern)
{
    sys_mutex_lock(policy->tree_lock);
    void *oldp = qd_parse_tree_remove_pattern_str(policy->hostname_tree, hostPattern);
    sys_mutex_unlock(policy->tree_lock);
    if (!oldp) {
        qd_log(policy->log_source, QD_LOG_WARNING, "vhost hostname pattern '%s' for removal not found", hostPattern);
    }
}


// Look up a hostname in the lookup parse_tree
char * qd_policy_host_pattern_lookup(qd_policy_t *policy, char *hostPattern)
{
    void *payload = 0;
    sys_mutex_lock(policy->tree_lock);
    bool matched = qd_parse_tree_retrieve_match_str(policy->hostname_tree, hostPattern, &payload);
    sys_mutex_unlock(policy->tree_lock);
    if (!matched) {
        payload = 0;
    }
    qd_log(policy->log_source, QD_LOG_TRACE, "vhost hostname pattern '%s' lookup returned '%s'", 
           hostPattern, (payload ? (char *)payload : "null"));
    return payload;
}
