use serde::de::{MapAccess, SeqAccess, Visitor};
use serde::{Deserialize, Deserializer};
use std::collections::BTreeSet;
use std::fmt;

struct NoDuplicateKeys;

impl<'de> Deserialize<'de> for NoDuplicateKeys {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        deserializer.deserialize_any(Self)
    }
}

impl<'de> Visitor<'de> for NoDuplicateKeys {
    type Value = Self;

    fn expecting(&self, formatter: &mut fmt::Formatter) -> fmt::Result {
        formatter.write_str("JSON without duplicate object keys")
    }

    fn visit_bool<E>(self, _: bool) -> Result<Self, E> {
        Ok(Self)
    }

    fn visit_i64<E>(self, _: i64) -> Result<Self, E> {
        Ok(Self)
    }

    fn visit_u64<E>(self, _: u64) -> Result<Self, E> {
        Ok(Self)
    }

    fn visit_f64<E>(self, _: f64) -> Result<Self, E> {
        Ok(Self)
    }

    fn visit_str<E>(self, _: &str) -> Result<Self, E> {
        Ok(Self)
    }

    fn visit_unit<E>(self) -> Result<Self, E> {
        Ok(Self)
    }

    fn visit_seq<A: SeqAccess<'de>>(self, mut sequence: A) -> Result<Self, A::Error> {
        while sequence.next_element::<Self>()?.is_some() {}
        Ok(Self)
    }

    fn visit_map<A: MapAccess<'de>>(self, mut object: A) -> Result<Self, A::Error> {
        let mut keys = BTreeSet::new();
        while let Some(key) = object.next_key::<String>()? {
            if !keys.insert(key.clone()) {
                return Err(serde::de::Error::custom(format!(
                    "duplicate JSON key: {key}"
                )));
            }
            object.next_value::<Self>()?;
        }
        Ok(Self)
    }
}

pub(super) fn validate(json: &str) -> Result<(), serde_json::Error> {
    serde_json::from_str::<NoDuplicateKeys>(json).map(|_| ())
}
