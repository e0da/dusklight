use channel_context::{dirty_paths, Catalog, Runtime};
use serde_json::json;
use std::sync::Arc;

const CATALOG: &str = include_str!("../../../config/channels.json");

#[test]
fn tracked_default_and_all_channels_share_the_same_resolver() {
    let catalog = Catalog::from_json(CATALOG).unwrap();
    let play = catalog.resolve(None).unwrap();
    assert_eq!(play["channel"], "play");
    assert_eq!(
        play["features"]["diagnostics"]["liveControl"]["enabled"],
        false
    );
    let lab = catalog.resolve(Some("lab")).unwrap();
    assert_eq!(
        lab["features"]["diagnostics"]["liveControl"]["enabled"],
        true
    );
    assert_eq!(lab["features"]["input"], play["features"]["input"]);
    assert_eq!(
        catalog.resolve_all()["channels"]["lab"]["features"],
        lab["features"]
    );
    assert!(catalog.resolve(Some("missing")).is_err());
}

#[test]
fn ordered_inheritance_deep_merges_objects_and_replaces_arrays_and_scalars() {
    let catalog = json!({
        "schemaVersion": 1, "defaultChannel": "test",
        "collections": {
            "first": {"features":{"a":1,"nested":{"a":1,"b":2},"list":[1,2]}},
            "second": {"extends":["first"],"features":{"a":2,"nested":{"b":3},"list":[3]}}
        },
        "channels": {"test":{"extends":["first","second"],"features":{"nested":{"a":4},"a":null}}}
    });
    let resolved = Catalog::from_json(&catalog.to_string())
        .unwrap()
        .resolve(None)
        .unwrap();
    assert_eq!(
        resolved["features"],
        json!({"a":null,"nested":{"a":4,"b":3},"list":[3]})
    );
    assert!(resolved.get("extends").is_none());
}

#[test]
fn rejects_invalid_catalogs_even_when_invalid_collection_is_unused() {
    for value in [
        json!({"schemaVersion":2,"defaultChannel":"a","collections":{},"channels":{"a":{}}}),
        json!({"schemaVersion":1,"defaultChannel":"missing","collections":{},"channels":{"a":{}}}),
        json!({"schemaVersion":1,"defaultChannel":"a","collections":{},"channels":{"a":{"extends":["missing"]}}}),
        json!({"schemaVersion":1,"defaultChannel":"a","collections":{"x":{"extends":["y"]},"y":{"extends":["x"]}},"channels":{"a":{}}}),
        json!({"schemaVersion":1,"defaultChannel":"a","collections":{"x":{"extends":["missing"]}},"channels":{"a":{}}}),
        json!({"schemaVersion":1,"defaultChannel":"a","collections":{},"channels":{"a":{"extends":"bad"}}}),
    ] {
        assert!(Catalog::from_json(&value.to_string()).is_err(), "{value}");
    }
}

#[test]
fn prepare_is_nonmutating_commit_is_atomic_and_stale_or_foreign_work_is_rejected() {
    let mut runtime = Runtime::new(Catalog::from_json(CATALOG).unwrap(), None).unwrap();
    let old = runtime.snapshot();
    let lab = runtime.prepare(Some("lab")).unwrap();
    let stale = runtime.prepare(None).unwrap();
    assert!(Arc::ptr_eq(&old, &runtime.snapshot()));
    assert_eq!(lab.snapshot().generation(), 1);
    assert!(lab
        .dirty_paths()
        .contains(&"/features/diagnostics/liveControl/enabled".into()));
    assert!(!lab
        .dirty_paths()
        .contains(&"/features/input/gyroAim/enabled".into()));
    runtime.commit(&lab).unwrap();
    assert_eq!(runtime.snapshot().channel(), "lab");
    assert_eq!(old.channel(), "play");
    assert!(runtime.commit(&stale).is_err());
    assert!(runtime.commit(&lab).is_err());
    let mut other = Runtime::new(Catalog::from_json(CATALOG).unwrap(), None).unwrap();
    assert!(other.commit(&stale).is_err());
    runtime.commit(&runtime.prepare(None).unwrap()).unwrap();
    assert_eq!(runtime.snapshot().generation(), 2);
    assert_eq!(runtime.snapshot().channel(), "play");
}

#[test]
fn scoped_overrides_preserve_parent_and_siblings_and_stable_scalar_ids() {
    let runtime = Runtime::new(Catalog::from_json(CATALOG).unwrap(), None).unwrap();
    let root = runtime.snapshot();
    let scoped = root
        .scoped(&json!({"features":{"input":{"gyroAim":{"enabled":false}}}}))
        .unwrap();
    let sibling = root.scoped(&json!({"tooling":{"telemetryHz":5}})).unwrap();
    assert!(Arc::ptr_eq(scoped.parent().unwrap(), &root));
    assert_eq!(
        scoped.get("features.input.gyroAim.enabled"),
        Some(&json!(false))
    );
    assert_eq!(
        root.get("features.input.gyroAim.enabled"),
        Some(&json!(true))
    );
    assert_eq!(
        sibling.get("features.input.gyroAim.enabled"),
        Some(&json!(true))
    );
    let id = root
        .columns()
        .paths
        .iter()
        .find(|(_, path)| path.as_str() == "/features/input/gyroAim/enabled")
        .unwrap()
        .0;
    assert!(root.columns().bools.contains(&(*id, true)));
    assert!(scoped.columns().bools.contains(&(*id, false)));
    assert!(root.scoped(&json!({"channel":"lab"})).is_err());
}

#[test]
fn dirty_paths_cover_added_removed_and_type_changed_leaves_with_escaped_keys() {
    assert_eq!(
        dirty_paths(
            &json!({"a":{"b":1},"gone":true,"x/y":1}),
            &json!({"a":false,"new":[1],"x/y":2})
        ),
        vec!["/a", "/a/b", "/gone", "/new", "/x~1y"]
    );
    assert!(dirty_paths(&json!({"a":1}), &json!({"a":1})).is_empty());
}
