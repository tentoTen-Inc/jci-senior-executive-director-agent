-- 取り込んだファイル（PDF・画像等）。メッセージごとに最新の処理結果（stored / deleted_unsent 等）
CREATE OR REPLACE VIEW `${PROJECT}.${DATASET}.v_files` AS
SELECT
  JSON_VALUE(data, '$.payload.message_id') AS message_id,
  JSON_VALUE(data, '$.payload.status') AS status,
  JSON_VALUE(data, '$.payload.source_type') AS source_type,
  JSON_VALUE(data, '$.payload.group_id') AS group_id,
  JSON_VALUE(data, '$.payload.user_id') AS user_id,
  TIMESTAMP_MILLIS(SAFE_CAST(JSON_VALUE(data, '$.payload.sent_at_ms') AS INT64)) AS sent_at,
  JSON_VALUE(data, '$.payload.file_name') AS file_name,
  JSON_VALUE(data, '$.payload.content_type') AS content_type,
  SAFE_CAST(JSON_VALUE(data, '$.payload.size') AS INT64) AS size,
  JSON_VALUE(data, '$.payload.sha256') AS sha256,
  JSON_VALUE(data, '$.payload.gcs_uri') AS gcs_uri,
  JSON_VALUE(data, '$.payload.text') AS text,
  received_at AS processed_at
FROM `${PROJECT}.${DATASET}.v_events`
WHERE kind = 'content'
QUALIFY ROW_NUMBER() OVER (
  PARTITION BY JSON_VALUE(data, '$.payload.message_id') ORDER BY received_at DESC
) = 1
