-- 受信メッセージとボットの送信を時系列に並べたもの（AI の応答品質の評価・改善用）
-- 返信（reply）は replyToken で受信イベントと突き合わせ、どの発言への返事かを残す。
CREATE OR REPLACE VIEW `${PROJECT}.${DATASET}.v_conversations` AS
WITH inbound_events AS (
  SELECT
    id AS event_id,
    event_type,
    JSON_VALUE(data, '$.payload.replyToken') AS reply_token,
    JSON_VALUE(data, '$.payload.source.type') AS source_type,
    JSON_VALUE(data, '$.payload.source.groupId') AS group_id,
    JSON_VALUE(data, '$.payload.source.roomId') AS room_id,
    JSON_VALUE(data, '$.payload.source.userId') AS user_id
  FROM `${PROJECT}.${DATASET}.v_events`
  WHERE kind = 'webhook' AND JSON_VALUE(data, '$.payload.replyToken') IS NOT NULL
),
outbound AS (
  SELECT
    'out' AS direction,
    e.received_at AS occurred_at,
    JSON_VALUE(e.data, '$.payload.channel') AS channel,
    COALESCE(i.source_type, 'user') AS source_type,
    i.group_id,
    i.room_id,
    COALESCE(JSON_VALUE(e.data, '$.payload.to'), i.user_id) AS user_id,
    JSON_VALUE(msg, '$.type') AS message_type,
    JSON_VALUE(msg, '$.text') AS text,
    CAST(NULL AS STRING) AS file_name,
    CAST(NULL AS BOOL) AS mentions_bot,
    i.event_id AS in_reply_to_event_id,
    i.event_type AS in_reply_to_event_type,
    e.id AS event_id
  FROM `${PROJECT}.${DATASET}.v_events` AS e,
    UNNEST(JSON_QUERY_ARRAY(e.data, '$.payload.messages')) AS msg
  LEFT JOIN inbound_events AS i
    ON i.reply_token = JSON_VALUE(e.data, '$.payload.reply_token')
  WHERE e.kind = 'outbound'
)
SELECT
  'in' AS direction,
  sent_at AS occurred_at,
  CAST(NULL AS STRING) AS channel,
  source_type,
  group_id,
  room_id,
  user_id,
  message_type,
  text,
  file_name,
  mentions_bot,
  CAST(NULL AS STRING) AS in_reply_to_event_id,
  CAST(NULL AS STRING) AS in_reply_to_event_type,
  event_id
FROM `${PROJECT}.${DATASET}.v_messages_for_ai`
UNION ALL
SELECT * FROM outbound
