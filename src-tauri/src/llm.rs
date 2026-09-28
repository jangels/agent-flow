use futures_util::StreamExt;
use serde_json::{json, Value};

/// Call an OpenAI-compatible `/chat/completions` endpoint with streaming enabled.
///
/// `on_token` is invoked for every streamed delta so the UI can render tokens live.
/// Returns the full concatenated assistant message.
pub async fn chat_stream(
    config: &super::AppConfig,
    system: &str,
    user: &str,
    mut on_token: impl FnMut(&str),
) -> Result<String, String> {
    let client = reqwest::Client::new();
    let base = config.base_url.trim_end_matches('/');
    let url = format!("{base}/chat/completions");

    let body = json!({
        "model": config.model,
        "temperature": config.temperature,
        "stream": true,
        "messages": [
            { "role": "system", "content": system },
            { "role": "user", "content": user }
        ]
    });

    let resp = client
        .post(&url)
        .bearer_auth(&config.api_key)
        .json(&body)
        .send()
        .await
        .map_err(|e| format!("请求失败: {e}"))?;

    if !resp.status().is_success() {
        let status = resp.status();
        let text = resp.text().await.unwrap_or_default();
        return Err(format!("LLM API 错误 {status}: {text}"));
    }

    let mut stream = resp.bytes_stream();
    let mut buf = String::new();
    let mut full = String::new();

    while let Some(chunk) = stream.next().await {
        let chunk = chunk.map_err(|e| format!("读取流失败: {e}"))?;
        buf.push_str(&String::from_utf8_lossy(&chunk));

        // SSE frames are separated by newlines; parse complete lines only.
        while let Some(idx) = buf.find('\n') {
            let line: String = buf.drain(..=idx).collect();
            let line = line.trim_end();
            if !line.starts_with("data:") {
                continue;
            }
            let data = line.trim_start_matches("data:").trim();
            if data == "[DONE]" {
                break;
            }
            if let Ok(v) = serde_json::from_str::<Value>(data) {
                if let Some(delta) = v["choices"]
                    .get(0)
                    .and_then(|c| c["delta"]["content"].as_str())
                {
                    full.push_str(delta);
                    on_token(delta);
                }
            }
        }
    }

    Ok(full)
}
