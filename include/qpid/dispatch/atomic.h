#ifndef __sys_atomic_h__
#define __sys_atomic_h__ 1
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

/**@file
 * Portable atomic operations on uint32_t.
 */

#include <stdint.h>

/******************************************************************************
 * C11 atomics                                                                *
 ******************************************************************************/
#if defined(__STDC__) && (__STDC_VERSION__ >= 201112L) && !defined(__STDC_NO_ATOMICS__)

#include <stdatomic.h>
typedef atomic_uint sys_atomic_t;

static inline void sys_atomic_init(sys_atomic_t * ref, uint32_t value)
{
    atomic_store(ref, value);
}

static inline uint32_t sys_atomic_add(sys_atomic_t * ref, uint32_t value)
{
    return atomic_fetch_add(ref, value);
}

static inline uint32_t sys_atomic_sub(sys_atomic_t * ref, uint32_t value)
{
    return atomic_fetch_sub(ref, value);
}

static inline uint32_t sys_atomic_get(sys_atomic_t * ref)
{
    return atomic_load(ref);
}

static inline void sys_atomic_destroy(sys_atomic_t * ref) {}

#elif defined(__GNUC__) || defined(__clang__)

/******************************************************************************
 * GCC specific atomics                                                       *
 ******************************************************************************/

typedef volatile uint32_t sys_atomic_t;

static inline void sys_atomic_init(sys_atomic_t * ref, uint32_t value)
{
    *ref = value;
}

static inline uint32_t sys_atomic_add(sys_atomic_t * ref, uint32_t value)
{
    return __sync_fetch_and_add(ref, value);
}

static inline uint32_t sys_atomic_sub(sys_atomic_t * ref, uint32_t value)
{
    return __sync_fetch_and_sub(ref, value);
}

static inline uint32_t sys_atomic_get(sys_atomic_t * ref)
{
    return *ref;
}

static inline void sys_atomic_destroy(sys_atomic_t * ref) {}

#else

/******************************************************************************
 * Mutex fallback atomics                                                     *
 ******************************************************************************/
#include <qpid/dispatch/threading.h>

struct sys_atomic_t {
    sys_mutex_t * lock;
    uint32_t value;
};
typedef struct sys_atomic_t sys_atomic_t;

static inline void sys_atomic_init(sys_atomic_t * ref, uint32_t value)
{
    ref->lock = sys_mutex();
    ref->value = value;
}

static inline uint32_t sys_atomic_add(sys_atomic_t * ref, uint32_t value)
{
    sys_mutex_lock(ref->lock);
    uint32_t prev = ref->value;
    ref->value += value;
    sys_mutex_unlock(ref->lock);
    return prev;
}

static inline uint32_t sys_atomic_sub(sys_atomic_t * ref, uint32_t value)
{
    sys_mutex_lock(ref->lock);
    uint32_t prev = ref->value;
    ref->value -= value;
    sys_mutex_unlock(ref->lock);
    return prev;
}

static inline uint32_t sys_atomic_get(sys_atomic_t * ref)
{
    sys_mutex_lock(ref->lock);
    uint32_t value = ref->value;
    sys_mutex_unlock(ref->lock);
    return value;
}

static inline void sys_atomic_destroy(sys_atomic_t * ref)
{
    sys_mutex_lock(ref->lock);
    sys_mutex_free(ref->lock);
}

#endif

#define sys_atomic_inc(ref) sys_atomic_add((ref), 1)
#define sys_atomic_dec(ref) sys_atomic_sub((ref), 1)

#endif
