use channel_context::Catalog;
use serde_json::Value;

fn run() -> Result<Value, String> {
    let mut args = std::env::args().skip(1);
    let operation = args.next().ok_or("Expected resolve or get")?;
    if operation != "resolve" && operation != "get" {
        return Err("Expected resolve or get".into());
    }
    let mut catalog = None;
    let mut channel = None;
    let mut all = false;
    let mut path = None;
    while let Some(flag) = args.next() {
        match flag.as_str() {
            "--catalog" => catalog = Some(args.next().ok_or("--catalog needs a path")?),
            "--channel" => channel = Some(args.next().ok_or("--channel needs a name")?),
            "--all" => all = true,
            "--path" => path = Some(args.next().ok_or("--path needs a dotted path")?),
            _ => return Err(format!("Unknown argument '{flag}'")),
        }
    }
    if all && (channel.is_some() || operation == "get") {
        return Err("--all cannot be combined with --channel or get".into());
    }
    let text = std::fs::read_to_string(catalog.ok_or("--catalog is required")?)
        .map_err(|error| error.to_string())?;
    let catalog = Catalog::from_json(&text)?;
    if all {
        return Ok(catalog.resolve_all());
    }
    let value = catalog.resolve(channel.as_deref())?;
    if operation == "get" {
        let path = path.ok_or("get requires --path")?;
        return path
            .split('.')
            .try_fold(&value, |value, key| value.get(key))
            .cloned()
            .ok_or_else(|| format!("Unknown path '{path}'"));
    }
    if path.is_some() {
        return Err("--path requires get".into());
    }
    Ok(value)
}

fn main() {
    match run() {
        Ok(value) => println!("{}", serde_json::to_string(&value).unwrap()),
        Err(error) => {
            eprintln!("channel-context: {error}");
            std::process::exit(1);
        }
    }
}
