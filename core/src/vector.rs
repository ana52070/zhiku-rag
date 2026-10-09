pub const CHUNK_SIZE: usize = 800;
pub const CHUNK_OVERLAP: usize = 120;
pub const MAX_CHUNKS: usize = 10000;

/// 文本按字符窗口分块，与 Python 算法一致
pub fn split_text(text: &str) -> Vec<String> {
    let mut pieces = Vec::new();
    let step = CHUNK_SIZE - CHUNK_OVERLAP;
    let chars: Vec<char> = text.chars().collect();
    let total = chars.len();

    let mut start = 0;
    while start < total {
        let end = (start + CHUNK_SIZE).min(total);
        let slice: String = chars[start..end].iter().collect();
        if !slice.trim().is_empty() {
            pieces.push(slice);
        }
        start += step;
    }
    pieces
}

/// 快速余弦相似度计算，循环利用 LLVM SIMD 向量化加速
#[inline]
pub fn cosine_similarity(query: &[f32], target: &[f32], query_norm: f32) -> f32 {
    if query.len() != target.len() || query_norm == 0.0 {
        return 0.0;
    }

    let mut dot = 0.0f32;
    let mut target_norm_sq = 0.0f32;

    for (&q, &t) in query.iter().zip(target.iter()) {
        dot += q * t;
        target_norm_sq += t * t;
    }

    let target_norm = target_norm_sq.sqrt();
    if target_norm == 0.0 {
        return 0.0;
    }

    let score = dot / (query_norm * target_norm);
    score.clamp(-1.0, 1.0)
}

pub fn calculate_norm(v: &[f32]) -> f32 {
    let sum_sq: f32 = v.iter().map(|&x| x * x).sum();
    sum_sq.sqrt()
}

pub fn validate_vectors(vectors: &[Vec<f32>], count: usize) -> Result<usize, String> {
    if vectors.is_empty() || vectors.len() != count {
        return Err("模型返回的向量数量与输入不一致。".to_string());
    }
    let mut dimension = 0;
    for v in vectors {
        if v.is_empty() || v.len() > 16384 {
            return Err("模型返回的向量格式或维度无效。".to_string());
        }
        if dimension != 0 && v.len() != dimension {
            return Err("模型返回的向量维度不一致。".to_string());
        }
        dimension = v.len();
        for &x in v {
            if !x.is_finite() {
                return Err("模型返回了无效数值。".to_string());
            }
        }
        let norm = calculate_norm(v);
        if !norm.is_finite() || norm == 0.0 {
            return Err("模型返回了零向量或超出范围的数值。".to_string());
        }
    }
    Ok(dimension)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_split_text() {
        let text = "a".repeat(1600);
        let chunks = split_text(&text);
        assert!(!chunks.is_empty());
        assert_eq!(chunks[0].len(), 800);
    }

    #[test]
    fn test_cosine_similarity() {
        let a = vec![1.0, 0.0, 0.0];
        let b = vec![1.0, 0.0, 0.0];
        let c = vec![0.0, 1.0, 0.0];
        let norm_a = calculate_norm(&a);
        assert!((cosine_similarity(&a, &b, norm_a) - 1.0).abs() < 1e-6);
        assert!((cosine_similarity(&a, &c, norm_a) - 0.0).abs() < 1e-6);
    }
}
