use crate::{Catalog, Runtime, Snapshot, Transition};
use std::cell::RefCell;
use std::ffi::{c_char, CStr, CString};
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::ptr;
use std::sync::Arc;

thread_local! {
    static LAST_ERROR: RefCell<CString> = RefCell::new(CString::default());
}

fn attempt<T>(operation: impl FnOnce() -> crate::Result<T>) -> Option<T> {
    match catch_unwind(AssertUnwindSafe(operation)) {
        Ok(Ok(value)) => Some(value),
        result => {
            let message = match result {
                Ok(Err(message)) => message,
                _ => "Channel context operation panicked".into(),
            };
            LAST_ERROR.with(|error| {
                *error.borrow_mut() = CString::new(message.replace('\0', "\\0")).unwrap()
            });
            None
        }
    }
}

unsafe fn text<'a>(value: *const c_char) -> crate::Result<&'a str> {
    if value.is_null() {
        return Err("Required string is null".into());
    }
    CStr::from_ptr(value)
        .to_str()
        .map_err(|error| error.to_string())
}

unsafe fn channel<'a>(value: *const c_char) -> crate::Result<Option<&'a str>> {
    if value.is_null() {
        Ok(None)
    } else {
        text(value).map(Some)
    }
}

pub struct CcRuntime(Runtime);

pub struct CcSnapshot {
    snapshot: Arc<Snapshot>,
    json: CString,
    channel: CString,
}

impl CcSnapshot {
    fn new(snapshot: Arc<Snapshot>) -> Self {
        Self {
            json: CString::new(snapshot.data.to_string()).unwrap(),
            channel: CString::new(snapshot.channel.clone()).unwrap(),
            snapshot,
        }
    }
}

pub struct CcTransition {
    transition: Transition,
    snapshot: CcSnapshot,
    dirty: CString,
}

#[no_mangle]
pub unsafe extern "C" fn cc_runtime_create(
    json: *const c_char,
    initial: *const c_char,
) -> *mut CcRuntime {
    attempt(|| {
        let catalog = Catalog::from_json(text(json)?)?;
        Runtime::new(catalog, channel(initial)?)
            .map(|runtime| Box::into_raw(Box::new(CcRuntime(runtime))))
    })
    .unwrap_or(ptr::null_mut())
}

#[no_mangle]
pub unsafe extern "C" fn cc_runtime_destroy(runtime: *mut CcRuntime) {
    if !runtime.is_null() {
        drop(Box::from_raw(runtime));
    }
}

#[no_mangle]
pub unsafe extern "C" fn cc_runtime_prepare(
    runtime: *mut CcRuntime,
    selected: *const c_char,
) -> *mut CcTransition {
    attempt(|| {
        let runtime = runtime.as_ref().ok_or("Runtime is null")?;
        let transition = runtime.0.prepare(channel(selected)?)?;
        let snapshot = CcSnapshot::new(Arc::clone(&transition.snapshot));
        let dirty = CString::new(serde_json::to_string(&transition.dirty_paths).unwrap()).unwrap();
        Ok(Box::into_raw(Box::new(CcTransition {
            transition,
            snapshot,
            dirty,
        })))
    })
    .unwrap_or(ptr::null_mut())
}

#[no_mangle]
pub unsafe extern "C" fn cc_runtime_commit(
    runtime: *mut CcRuntime,
    transition: *const CcTransition,
) -> bool {
    attempt(|| {
        let runtime = runtime.as_mut().ok_or("Runtime is null")?;
        let transition = transition.as_ref().ok_or("Transition is null")?;
        runtime.0.commit(&transition.transition)
    })
    .is_some()
}

#[no_mangle]
pub unsafe extern "C" fn cc_runtime_snapshot(runtime: *const CcRuntime) -> *mut CcSnapshot {
    attempt(|| {
        let runtime = runtime.as_ref().ok_or("Runtime is null")?;
        Ok(Box::into_raw(Box::new(CcSnapshot::new(
            runtime.0.snapshot(),
        ))))
    })
    .unwrap_or(ptr::null_mut())
}

#[no_mangle]
pub unsafe extern "C" fn cc_snapshot_destroy(snapshot: *mut CcSnapshot) {
    if !snapshot.is_null() {
        drop(Box::from_raw(snapshot));
    }
}

#[no_mangle]
pub unsafe extern "C" fn cc_transition_destroy(transition: *mut CcTransition) {
    if !transition.is_null() {
        drop(Box::from_raw(transition));
    }
}

#[no_mangle]
pub unsafe extern "C" fn cc_snapshot_json(snapshot: *const CcSnapshot) -> *const c_char {
    snapshot
        .as_ref()
        .map_or(ptr::null(), |value| value.json.as_ptr())
}

#[no_mangle]
pub unsafe extern "C" fn cc_snapshot_channel(snapshot: *const CcSnapshot) -> *const c_char {
    snapshot
        .as_ref()
        .map_or(ptr::null(), |value| value.channel.as_ptr())
}

#[no_mangle]
pub unsafe extern "C" fn cc_snapshot_generation(snapshot: *const CcSnapshot) -> u64 {
    snapshot
        .as_ref()
        .map_or(0, |value| value.snapshot.generation)
}

#[no_mangle]
pub unsafe extern "C" fn cc_transition_json(transition: *const CcTransition) -> *const c_char {
    transition
        .as_ref()
        .map_or(ptr::null(), |value| value.snapshot.json.as_ptr())
}

#[no_mangle]
pub unsafe extern "C" fn cc_transition_channel(transition: *const CcTransition) -> *const c_char {
    transition
        .as_ref()
        .map_or(ptr::null(), |value| value.snapshot.channel.as_ptr())
}

#[no_mangle]
pub unsafe extern "C" fn cc_transition_generation(transition: *const CcTransition) -> u64 {
    transition
        .as_ref()
        .map_or(0, |value| value.snapshot.snapshot.generation)
}

#[no_mangle]
pub unsafe extern "C" fn cc_transition_dirty_paths_json(
    transition: *const CcTransition,
) -> *const c_char {
    transition
        .as_ref()
        .map_or(ptr::null(), |value| value.dirty.as_ptr())
}

#[no_mangle]
pub extern "C" fn cc_last_error() -> *const c_char {
    LAST_ERROR.with(|error| error.borrow().as_ptr())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ffi_handles_preserve_borrowed_data_and_reject_stale_commits() {
        let catalog = CString::new(include_str!("../../../config/channels.json")).unwrap();
        let lab = CString::new("lab").unwrap();
        unsafe {
            let runtime = cc_runtime_create(catalog.as_ptr(), ptr::null());
            assert!(!runtime.is_null());
            let old = cc_runtime_snapshot(runtime);
            let transition = cc_runtime_prepare(runtime, lab.as_ptr());
            assert_eq!(
                CStr::from_ptr(cc_transition_channel(transition))
                    .to_str()
                    .unwrap(),
                "lab"
            );
            assert_eq!(cc_transition_generation(transition), 1);
            assert!(cc_runtime_commit(runtime, transition));
            assert!(!cc_runtime_commit(runtime, transition));
            let error = CStr::from_ptr(cc_last_error()).to_owned();
            assert!(error.to_str().unwrap().contains("Stale"));
            cc_runtime_destroy(runtime);
            assert_eq!(cc_snapshot_generation(old), 0);
            assert_eq!(
                CStr::from_ptr(cc_snapshot_channel(old)).to_str().unwrap(),
                "play"
            );
            assert!(CStr::from_ptr(cc_snapshot_json(old))
                .to_str()
                .unwrap()
                .contains("features"));
            assert!(CStr::from_ptr(cc_transition_dirty_paths_json(transition))
                .to_str()
                .unwrap()
                .contains("/features/diagnostics/liveControl/enabled"));
            assert_eq!(CStr::from_ptr(cc_last_error()), error.as_c_str());
            cc_snapshot_destroy(old);
            cc_transition_destroy(transition);
            assert!(cc_runtime_create(ptr::null(), ptr::null()).is_null());
            cc_runtime_destroy(ptr::null_mut());
        }
    }
}
