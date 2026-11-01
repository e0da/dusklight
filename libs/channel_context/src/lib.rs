use serde::Deserialize;
use serde_json::{Map, Value};
use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;

mod ffi;

pub type Result<T> = std::result::Result<T, String>;

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct CatalogInput {
    schema_version: u32,
    default_channel: String,
    collections: BTreeMap<String, Definition>,
    channels: BTreeMap<String, Definition>,
}

#[derive(Deserialize)]
struct Definition {
    #[serde(default)]
    extends: Vec<String>,
    #[serde(flatten)]
    values: Map<String, Value>,
}

#[derive(Debug)]
pub struct Catalog {
    default_channel: String,
    channels: BTreeMap<String, Value>,
}

pub fn deep_merge(target: &mut Value, patch: &Value) {
    if let (Some(target), Some(patch)) = (target.as_object_mut(), patch.as_object()) {
        for (key, value) in patch {
            deep_merge(target.entry(key.clone()).or_insert(Value::Null), value);
        }
    } else {
        *target = patch.clone();
    }
}

fn resolve_collection(
    name: &str,
    definitions: &BTreeMap<String, Definition>,
    resolved: &mut BTreeMap<String, Value>,
    visiting: &mut Vec<String>,
) -> Result<Value> {
    if let Some(value) = resolved.get(name) {
        return Ok(value.clone());
    }
    if visiting.iter().any(|entry| entry == name) {
        return Err(format!(
            "Collection inheritance cycle: {} -> {name}",
            visiting.join(" -> ")
        ));
    }
    let definition = definitions
        .get(name)
        .ok_or_else(|| format!("Unknown collection '{name}'"))?;
    visiting.push(name.into());
    let mut value = Value::Object(Map::new());
    for parent in &definition.extends {
        deep_merge(
            &mut value,
            &resolve_collection(parent, definitions, resolved, visiting)?,
        );
    }
    deep_merge(&mut value, &Value::Object(definition.values.clone()));
    visiting.pop();
    resolved.insert(name.into(), value.clone());
    Ok(value)
}

impl Catalog {
    pub fn from_json(json: &str) -> Result<Self> {
        let input: CatalogInput = serde_json::from_str(json).map_err(|error| error.to_string())?;
        if input.schema_version != 1 {
            return Err(format!(
                "Unsupported schemaVersion {}",
                input.schema_version
            ));
        }
        if !input.channels.contains_key(&input.default_channel) {
            return Err(format!(
                "Unknown defaultChannel '{}'",
                input.default_channel
            ));
        }
        for (name, definition) in input.collections.iter().chain(input.channels.iter()) {
            if name.is_empty() || name.contains('\0') {
                return Err("Channel and collection names must be nonempty without NUL".into());
            }
            if definition.values.contains_key("channel") {
                return Err(format!("'{name}' uses reserved field 'channel'"));
            }
        }
        let mut collections = BTreeMap::new();
        for name in input.collections.keys() {
            resolve_collection(name, &input.collections, &mut collections, &mut Vec::new())?;
        }
        let mut channels = BTreeMap::new();
        for (name, definition) in input.channels {
            let mut value = Value::Object(Map::new());
            for parent in &definition.extends {
                let parent_value = collections
                    .get(parent)
                    .ok_or_else(|| format!("Unknown collection '{parent}' in channel '{name}'"))?;
                deep_merge(&mut value, parent_value);
            }
            deep_merge(&mut value, &Value::Object(definition.values));
            channels.insert(name, value);
        }
        Ok(Self {
            default_channel: input.default_channel,
            channels,
        })
    }

    pub fn default_channel(&self) -> &str {
        &self.default_channel
    }

    pub fn resolve(&self, channel: Option<&str>) -> Result<Value> {
        let channel = channel.unwrap_or(&self.default_channel);
        let mut value = self
            .channels
            .get(channel)
            .ok_or_else(|| format!("Unknown channel '{channel}'"))?
            .clone();
        value
            .as_object_mut()
            .unwrap()
            .insert("channel".into(), channel.into());
        Ok(value)
    }

    pub fn resolve_all(&self) -> Value {
        serde_json::json!({"schemaVersion": 1, "defaultChannel": self.default_channel, "channels": self.channels})
    }
}

#[derive(Debug, Default)]
pub struct ScalarColumns {
    pub paths: BTreeMap<u64, String>,
    pub bools: Vec<(u64, bool)>,
    pub integers: Vec<(u64, i64)>,
    pub unsigned: Vec<(u64, u64)>,
    pub floats: Vec<(u64, f64)>,
    pub strings: Vec<(u64, String)>,
    pub nulls: Vec<u64>,
}

fn leaf_values<'a>(value: &'a Value, path: &str, out: &mut BTreeMap<String, &'a Value>) {
    if let Some(object) = value.as_object().filter(|object| !object.is_empty()) {
        for (key, value) in object {
            // JSON Pointer escaping keeps IDs and dirty paths unambiguous for every valid key.
            let key = key.replace('~', "~0").replace('/', "~1");
            leaf_values(value, &format!("{path}/{key}"), out);
        }
    } else {
        out.insert(path.into(), value);
    }
}

impl ScalarColumns {
    fn from_value(value: &Value) -> Result<Self> {
        let mut leaves = BTreeMap::new();
        leaf_values(value, "", &mut leaves);
        let mut result = Self::default();
        for (path, value) in leaves {
            let id = path.bytes().fold(0xcbf29ce484222325u64, |hash, byte| {
                (hash ^ u64::from(byte)).wrapping_mul(0x100000001b3)
            });
            if result.paths.insert(id, path).is_some() {
                return Err("Scalar path ID collision".into());
            }
            match value {
                Value::Bool(value) => result.bools.push((id, *value)),
                Value::Number(value) if value.is_i64() => {
                    result.integers.push((id, value.as_i64().unwrap()))
                }
                Value::Number(value) if value.is_u64() => {
                    result.unsigned.push((id, value.as_u64().unwrap()))
                }
                Value::Number(value) => result.floats.push((id, value.as_f64().unwrap())),
                Value::String(value) => result.strings.push((id, value.clone())),
                Value::Null => result.nulls.push(id),
                _ => {}
            }
        }
        Ok(result)
    }
}

#[derive(Debug)]
pub struct Snapshot {
    generation: u64,
    channel: String,
    data: Value,
    columns: ScalarColumns,
    parent: Option<Arc<Snapshot>>,
}

impl Snapshot {
    pub fn generation(&self) -> u64 {
        self.generation
    }
    pub fn channel(&self) -> &str {
        &self.channel
    }
    pub fn data(&self) -> &Value {
        &self.data
    }
    pub fn columns(&self) -> &ScalarColumns {
        &self.columns
    }
    pub fn parent(&self) -> Option<&Arc<Self>> {
        self.parent.as_ref()
    }

    fn new(generation: u64, data: Value, parent: Option<Arc<Self>>) -> Result<Arc<Self>> {
        let channel = data["channel"]
            .as_str()
            .ok_or("Snapshot channel must be a string")?
            .into();
        let columns = ScalarColumns::from_value(&data)?;
        Ok(Arc::new(Self {
            generation,
            channel,
            data,
            columns,
            parent,
        }))
    }

    pub fn scoped(self: &Arc<Self>, overrides: &Value) -> Result<Arc<Self>> {
        if !overrides.is_object() || overrides.get("channel").is_some() {
            return Err(
                "Scoped overrides must be an object without reserved field 'channel'".into(),
            );
        }
        let mut data = self.data.clone();
        deep_merge(&mut data, overrides);
        Self::new(self.generation, data, Some(Arc::clone(self)))
    }

    pub fn get(&self, dotted_path: &str) -> Option<&Value> {
        dotted_path
            .split('.')
            .try_fold(&self.data, |value, key| value.get(key))
    }
}

pub fn dirty_paths(before: &Value, after: &Value) -> Vec<String> {
    let mut old = BTreeMap::new();
    let mut new = BTreeMap::new();
    leaf_values(before, "", &mut old);
    leaf_values(after, "", &mut new);
    old.keys()
        .chain(new.keys())
        .collect::<BTreeSet<_>>()
        .into_iter()
        .filter(|path| old.get(*path) != new.get(*path))
        .cloned()
        .collect()
}

pub struct Transition {
    base: Arc<Snapshot>,
    snapshot: Arc<Snapshot>,
    dirty_paths: Vec<String>,
}

impl Transition {
    pub fn snapshot(&self) -> &Arc<Snapshot> {
        &self.snapshot
    }
    pub fn dirty_paths(&self) -> &[String] {
        &self.dirty_paths
    }
}

pub struct Runtime {
    catalog: Catalog,
    current: Arc<Snapshot>,
}

impl Runtime {
    pub fn new(catalog: Catalog, initial_channel: Option<&str>) -> Result<Self> {
        let current = Snapshot::new(0, catalog.resolve(initial_channel)?, None)?;
        Ok(Self { catalog, current })
    }

    pub fn snapshot(&self) -> Arc<Snapshot> {
        Arc::clone(&self.current)
    }

    pub fn prepare(&self, channel: Option<&str>) -> Result<Transition> {
        let generation = self
            .current
            .generation
            .checked_add(1)
            .ok_or("Generation exhausted")?;
        let snapshot = Snapshot::new(generation, self.catalog.resolve(channel)?, None)?;
        let dirty_paths = dirty_paths(&self.current.data, &snapshot.data);
        Ok(Transition {
            base: Arc::clone(&self.current),
            snapshot,
            dirty_paths,
        })
    }

    pub fn commit(&mut self, transition: &Transition) -> Result<()> {
        if !Arc::ptr_eq(&self.current, &transition.base) {
            return Err("Stale or foreign transition".into());
        }
        self.current = Arc::clone(&transition.snapshot);
        Ok(())
    }
}
