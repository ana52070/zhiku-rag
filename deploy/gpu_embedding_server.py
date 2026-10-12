import os
os.environ['HF_ENDPOINT'] = 'https://hf-mirror.com'
from fastapi import FastAPI
from pydantic import BaseModel
from sentence_transformers import SentenceTransformer
from typing import List, Union
import uvicorn

app = FastAPI(title="ZHIKU GPU Embedding Server")
print("Loading BAAI/bge-m3 onto NVIDIA GeForce RTX 3080...")
model = SentenceTransformer('BAAI/bge-m3', device='cuda')
print("Model loaded and ready on RTX 3080!")

class EmbeddingRequest(BaseModel):
    model: str = "bge-m3"
    input: Union[str, List[str]]

@app.post("/v1/embeddings")
@app.post("/embeddings")
def get_embeddings(req: EmbeddingRequest):
    texts = [req.input] if isinstance(req.input, str) else req.input
    vectors = model.encode(texts, batch_size=32, normalize_embeddings=True)
    data = []
    for idx, vec in enumerate(vectors):
        data.append({
            "object": "embedding",
            "index": idx,
            "embedding": vec.tolist()
        })
    return {
        "object": "list",
        "data": data,
        "model": req.model,
        "usage": {
            "prompt_tokens": sum(len(t) for t in texts),
            "total_tokens": sum(len(t) for t in texts)
        }
    }

@app.get("/health")
def health():
    return {"status": "ok", "device": "cuda", "model": "bge-m3"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=11435)
