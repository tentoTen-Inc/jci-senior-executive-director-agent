-- AI の回答と最新の評価（👍/👎）。応答品質の振り返りと評価データの元
CREATE OR REPLACE VIEW `${PROJECT}.${DATASET}.v_ai_answers` AS
WITH answers AS (
  SELECT
    JSON_VALUE(data, '$.payload.answer_id') AS answer_id,
    received_at AS answered_at,
    JSON_VALUE(data, '$.payload.member_id') AS member_id,
    JSON_VALUE(data, '$.payload.user_id') AS user_id,
    JSON_VALUE(data, '$.payload.question') AS question,
    JSON_VALUE(data, '$.payload.answer') AS answer,
    TO_JSON_STRING(JSON_QUERY(data, '$.payload.sources')) AS sources,
    ARRAY_LENGTH(JSON_QUERY_ARRAY(data, '$.payload.sources')) AS source_count,
    JSON_VALUE(data, '$.payload.grounded') = 'true' AS grounded,
    JSON_VALUE(data, '$.payload.needs_human') = 'true' AS needs_human,
    JSON_VALUE(data, '$.payload.model') AS model,
    SAFE_CAST(JSON_VALUE(data, '$.payload.input_tokens') AS INT64) AS input_tokens,
    SAFE_CAST(JSON_VALUE(data, '$.payload.output_tokens') AS INT64) AS output_tokens
  FROM `${PROJECT}.${DATASET}.v_events`
  WHERE kind = 'ai_answer'
),
feedback AS (
  SELECT
    JSON_VALUE(data, '$.payload.answer_id') AS answer_id,
    JSON_VALUE(data, '$.payload.rating') AS rating,
    received_at AS rated_at
  FROM `${PROJECT}.${DATASET}.v_events`
  WHERE kind = 'ai_feedback'
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY JSON_VALUE(data, '$.payload.answer_id') ORDER BY received_at DESC
  ) = 1
)
SELECT a.*, f.rating, f.rated_at
FROM answers AS a
LEFT JOIN feedback AS f USING (answer_id)
