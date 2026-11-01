use channel_context::Catalog;
use serde_json::Value;
use std::process::Command;

fn cli(args: &[&str]) -> std::process::Output {
    Command::new(env!("CARGO_BIN_EXE_channel-context"))
        .args(args)
        .arg("--catalog")
        .arg(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../../config/channels.json"
        ))
        .output()
        .unwrap()
}

#[test]
fn cli_default_all_and_get_use_the_runtime_resolver() {
    let catalog = Catalog::from_json(include_str!("../../../config/channels.json")).unwrap();
    for (args, expected) in [
        (vec!["resolve"], catalog.resolve(None).unwrap()),
        (
            vec!["resolve", "--channel", "lab"],
            catalog.resolve(Some("lab")).unwrap(),
        ),
        (vec!["resolve", "--all"], catalog.resolve_all()),
        (
            vec!["get", "--path", "features.input.gyroAim.enabled"],
            Value::Bool(true),
        ),
    ] {
        let output = cli(&args);
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        assert_eq!(
            serde_json::from_slice::<Value>(&output.stdout).unwrap(),
            expected
        );
    }
}

#[test]
fn cli_failures_use_stderr_and_nonzero_exit_without_partial_json() {
    for args in [
        vec!["resolve", "--all", "--channel", "lab"],
        vec!["resolve", "--channel", "missing"],
        vec!["get", "--path", "missing.path"],
        vec!["resolve", "--unknown"],
    ] {
        let output = cli(&args);
        assert!(!output.status.success());
        assert!(output.stdout.is_empty());
        assert!(!output.stderr.is_empty());
    }
}

#[test]
fn cli_rejects_duplicate_keys_without_partial_json() {
    let path = std::env::temp_dir().join(format!(
        "channel-context-duplicate-{}.json",
        std::process::id()
    ));
    std::fs::write(&path,
        r#"{"schemaVersion":1,"defaultChannel":"x","collections":{},"channels":{"x":{"features":{"enabled":true,"enabled":false}}}}"#,
    ).unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_channel-context"))
        .args(["resolve", "--catalog"])
        .arg(&path)
        .output()
        .unwrap();
    std::fs::remove_file(path).unwrap();
    assert!(!output.status.success());
    assert!(output.stdout.is_empty());
    assert!(String::from_utf8_lossy(&output.stderr).contains("duplicate JSON key: enabled"));
}
