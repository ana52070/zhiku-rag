use std::path::Path;
use std::time::{SystemTime, UNIX_EPOCH};
use aes::cipher::{block_padding::Pkcs7, BlockDecryptMut, BlockEncryptMut, KeyIvInit};
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine;
use hmac::{Hmac, Mac};
use rand::RngCore;
use sha2::Sha256;

type Aes128CbcEnc = cbc::Encryptor<aes::Aes128>;
type Aes128CbcDec = cbc::Decryptor<aes::Aes128>;
type HmacSha256 = Hmac<Sha256>;

#[derive(Clone)]
pub struct FernetCipher {
    signing_key: [u8; 16],
    encryption_key: [u8; 16],
}

impl FernetCipher {
    pub fn from_base64_key(key_b64: &str) -> Result<Self, String> {
        let trimmed = key_b64.trim().trim_end_matches('=');
        let key_bytes = URL_SAFE_NO_PAD
            .decode(trimmed)
            .map_err(|e| format!("Base64 解码密钥失败: {}", e))?;
        if key_bytes.len() != 32 {
            return Err(format!("Fernet 密钥长度必须是 32 字节，当前为 {}", key_bytes.len()));
        }
        let mut signing_key = [0u8; 16];
        let mut encryption_key = [0u8; 16];
        signing_key.copy_from_slice(&key_bytes[0..16]);
        encryption_key.copy_from_slice(&key_bytes[16..32]);
        Ok(Self {
            signing_key,
            encryption_key,
        })
    }

    pub fn load_or_create(key_path: &Path) -> Result<Self, String> {
        if key_path.exists() {
            let content = std::fs::read_to_string(key_path)
                .map_err(|e| format!("读取 secret.key 失败: {}", e))?;
            Self::from_base64_key(&content)
        } else {
            let mut key = [0u8; 32];
            rand::thread_rng().fill_bytes(&mut key);
            let b64 = URL_SAFE_NO_PAD.encode(key);
            std::fs::write(key_path, &b64)
                .map_err(|e| format!("写入 secret.key 失败: {}", e))?;
            Self::from_base64_key(&b64)
        }
    }

    pub fn encrypt(&self, data: &[u8]) -> Result<String, String> {
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs();
        let mut iv = [0u8; 16];
        rand::thread_rng().fill_bytes(&mut iv);

        let encryptor = Aes128CbcEnc::new(&self.encryption_key.into(), &iv.into());
        let mut buf = vec![0u8; data.len() + 16];
        let ct = encryptor
            .encrypt_padded_b2b_mut::<Pkcs7>(data, &mut buf)
            .map_err(|e| format!("AES 加密失败: {}", e))?;
        let ct_len = ct.len();

        let mut basic_parts = Vec::with_capacity(1 + 8 + 16 + ct_len);
        basic_parts.push(0x80);
        basic_parts.extend_from_slice(&now.to_be_bytes());
        basic_parts.extend_from_slice(&iv);
        basic_parts.extend_from_slice(&buf[..ct_len]);

        let mut mac = HmacSha256::new_from_slice(&self.signing_key)
            .map_err(|e| format!("HMAC 初始化失败: {}", e))?;
        mac.update(&basic_parts);
        let hmac_res = mac.finalize().into_bytes();

        let mut token = basic_parts;
        token.extend_from_slice(&hmac_res);

        Ok(URL_SAFE_NO_PAD.encode(token))
    }

    pub fn decrypt(&self, token_b64: &str) -> Result<Vec<u8>, String> {
        let trimmed = token_b64.trim().trim_end_matches('=');
        let token = URL_SAFE_NO_PAD
            .decode(trimmed)
            .map_err(|e| format!("Token Base64 解码失败: {}", e))?;

        if token.len() < 73 {
            return Err("Token 长度过短".to_string());
        }
        if token[0] != 0x80 {
            return Err("Token 版本不支持".to_string());
        }

        let payload_len = token.len() - 32;
        let payload = &token[..payload_len];
        let expected_hmac = &token[payload_len..];

        let mut mac = HmacSha256::new_from_slice(&self.signing_key)
            .map_err(|e| format!("HMAC 初始化失败: {}", e))?;
        mac.update(payload);
        mac.verify_slice(expected_hmac)
            .map_err(|_| "HMAC 校验失败，密文已被篡改".to_string())?;

        let iv: [u8; 16] = payload[9..25].try_into().unwrap();
        let ct = &payload[25..];

        let decryptor = Aes128CbcDec::new(&self.encryption_key.into(), &iv.into());
        let mut buf = vec![0u8; ct.len()];
        let pt = decryptor
            .decrypt_padded_b2b_mut::<Pkcs7>(ct, &mut buf)
            .map_err(|e| format!("AES 解密填充错误: {}", e))?;

        Ok(pt.to_vec())
    }
}

pub fn sha256_hex(input: &[u8]) -> String {
    use sha2::Digest;
    let mut hasher = Sha256::new();
    hasher.update(input);
    hex::encode(hasher.finalize())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_fernet_roundtrip() {
        let key = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=";
        let cipher = FernetCipher::from_base64_key(key).unwrap();
        let original = b"sk-test-openai-key-123456";
        let encrypted = cipher.encrypt(original).unwrap();
        let decrypted = cipher.decrypt(&encrypted).unwrap();
        assert_eq!(decrypted, original);
    }

    #[test]
    fn test_decrypt_python_fernet() {
        let key = "MkYk6m6YjrmadIcmsewrQ4qeEscy3bghKg89Yu4RdjU=";
        let token = "gAAAAABqyN3P62-vV8IZ04518ZmKWOVc5hqsDdld82yvPxp_aFvM-yuD8ZBSZicEfAb5-m1QWEPp8-f4lHxsKmz_WwkLZnaaVnr44oYNpeZYt43NUEAb_wo=";
        let cipher = FernetCipher::from_base64_key(key).unwrap();
        let decrypted = cipher.decrypt(token).unwrap();
        assert_eq!(String::from_utf8_lossy(&decrypted), "hello from python fernet");
    }
}
