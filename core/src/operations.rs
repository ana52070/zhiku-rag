use std::collections::HashMap;
use std::sync::{Arc, RwLock};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OperationItem {
    pub id: String,
    pub kind: String,
    pub state: String,
    pub stage: String,
    pub filename: String,
    pub total: i64,
    pub completed: i64,
    pub failed: i64,
    pub current: i64,
    pub stage_total: Option<i64>,
    pub error: String,
    pub updated: f64,
}

#[derive(Clone, Default)]
pub struct Operations {
    items: Arc<RwLock<HashMap<String, OperationItem>>>,
}

impl Operations {
    pub fn new() -> Self {
        Self {
            items: Arc::new(RwLock::new(HashMap::new())),
        }
    }

    pub fn get(&self, id: &str) -> Option<OperationItem> {
        self.items.read().unwrap().get(id).cloned()
    }

    pub fn update(&self, item: OperationItem) {
        let mut map = self.items.write().unwrap();
        map.insert(item.id.clone(), item);
    }
}
