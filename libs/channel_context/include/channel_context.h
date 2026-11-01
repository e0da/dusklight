#ifndef DUSK_CHANNEL_CONTEXT_H
#define DUSK_CHANNEL_CONTEXT_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct cc_runtime cc_runtime;
typedef struct cc_snapshot cc_snapshot;
typedef struct cc_transition cc_transition;

/* Handles are caller-owned and must be destroyed once. Runtime calls require external
 * synchronization. Snapshot and transition getters return borrowed strings valid until destroy.
 * Null channel selects the catalog default. Failed operations return null/false and set last_error.
 * Input strings must be NUL-terminated; invalid UTF-8 returns an error. Null destruction is allowed.
 * Passing invalid handles or using a handle after destroy is not supported. */
cc_runtime* cc_runtime_create(const char* catalog_json, const char* initial_channel);
void cc_runtime_destroy(cc_runtime* runtime);
cc_transition* cc_runtime_prepare(cc_runtime* runtime, const char* channel);
bool cc_runtime_commit(cc_runtime* runtime, const cc_transition* transition);
cc_snapshot* cc_runtime_snapshot(const cc_runtime* runtime);

void cc_transition_destroy(cc_transition* transition);
const char* cc_transition_json(const cc_transition* transition);
const char* cc_transition_dirty_paths_json(const cc_transition* transition);
const char* cc_transition_channel(const cc_transition* transition);
uint64_t cc_transition_generation(const cc_transition* transition);

void cc_snapshot_destroy(cc_snapshot* snapshot);
const char* cc_snapshot_json(const cc_snapshot* snapshot);
const char* cc_snapshot_channel(const cc_snapshot* snapshot);
uint64_t cc_snapshot_generation(const cc_snapshot* snapshot);

/* Thread-local error text, valid until the next failing operation on this thread. */
const char* cc_last_error(void);

#ifdef __cplusplus
}
#endif
#endif
