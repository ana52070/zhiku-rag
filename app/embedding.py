import math
import json

import httpx
from .progress import report


def validate_vectors(vectors, count):
    if not isinstance(vectors, list) or not vectors or len(vectors) != count:
        raise ValueError("模型返回的向量数量与输入不一致。")
    dimension = 0
    for vector in vectors:
        if not isinstance(vector, list) or not vector or len(vector) > 16384:
            raise ValueError("模型返回的向量格式或维度无效。")
        if dimension and len(vector) != dimension:
            raise ValueError("模型返回的向量维度不一致。")
        dimension = len(vector)
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in vector):
            raise ValueError("模型返回了无效数值。")
        norm = math.sqrt(sum(value * value for value in vector))
        if not math.isfinite(norm) or norm == 0:
            raise ValueError("模型返回了零向量或超出范围的数值。")
    return dimension


class HTTPEmbedding:
    async def embed(self, config, texts):
        result = []
        headers = {"Authorization": f'Bearer {config["api_key"]}'} if config["api_key"] else {}
        try:
            async with httpx.AsyncClient(timeout=45, follow_redirects=False) as client:
                for start in range(0, len(texts), 32):
                    batch = texts[start:start + 32]
                    response = await client.post(config["base_url"].rstrip("/") + "/embeddings", headers=headers, json={"model": config["model"], "input": batch})
                    if response.status_code in {401, 403}:
                        raise ValueError("模型接口拒绝访问，请检查 API Key 和权限。")
                    if response.status_code == 429:
                        raise ValueError("模型接口限流或额度不足，请稍后重试。")
                    if not response.is_success:
                        raise ValueError(f"模型接口返回 HTTP {response.status_code}，请检查地址和模型名称。")
                    data = response.json()["data"]
                    ordered = sorted(data, key=lambda item: item["index"])
                    if [item["index"] for item in ordered] != list(range(len(batch))):
                        raise ValueError("模型接口返回的向量索引不完整。")
                    vectors = [item["embedding"] for item in ordered]
                    validate_vectors(vectors, len(batch))
                    result.extend(vectors)
                    report(stage='embedding',current=len(result),stage_total=len(texts))
        except httpx.TimeoutException:
            raise ValueError("模型接口响应超时，请检查网络或缩小文件后重试。") from None
        except httpx.RequestError:
            raise ValueError("模型接口连接失败，请检查地址和网络。") from None
        except (KeyError, TypeError):
            raise ValueError("模型接口响应格式不兼容，需要返回 data/index/embedding。") from None
        except ValueError as error:
            if isinstance(error, json.JSONDecodeError):
                raise ValueError("模型接口未返回有效 JSON，请检查接口基础地址。") from None
            raise
        validate_vectors(result, len(texts))
        return result
