-- 受信メッセージ（グループ・個別の全メッセージ。送信取消済みは unsent=TRUE）
CREATE OR REPLACE VIEW `${PROJECT}.${DATASET}.v_messages` AS
WITH unsent AS (
  SELECT DISTINCT JSON_VALUE(data, '$.payload.unsend.messageId') AS message_id
  FROM `${PROJECT}.${DATASET}.v_events`
  WHERE kind = 'webhook' AND event_type = 'unsend'
)
SELECT
  e.id AS event_id,
  TIMESTAMP_MILLIS(SAFE_CAST(JSON_VALUE(e.data, '$.payload.timestamp') AS INT64)) AS sent_at,
  e.received_at,
  JSON_VALUE(e.data, '$.payload.source.type') AS source_type,
  JSON_VALUE(e.data, '$.payload.source.groupId') AS group_id,
  JSON_VALUE(e.data, '$.payload.source.roomId') AS room_id,
  JSON_VALUE(e.data, '$.payload.source.userId') AS user_id,
  JSON_VALUE(e.data, '$.payload.message.id') AS message_id,
  JSON_VALUE(e.data, '$.payload.message.type') AS message_type,
  JSON_VALUE(e.data, '$.payload.message.text') AS text,
  JSON_VALUE(e.data, '$.payload.message.fileName') AS file_name,
  SAFE_CAST(JSON_VALUE(e.data, '$.payload.message.fileSize') AS INT64) AS file_size,
  JSON_VALUE(e.data, '$.payload.message.quotedMessageId') AS quoted_message_id,
  JSON_VALUE(e.data, '$.payload.replyToken') AS reply_token,
  JSON_VALUE(e.data, '$.payload.message.packageId') AS sticker_package_id,
  JSON_VALUE(e.data, '$.payload.message.stickerId') AS sticker_id,
  EXISTS (
    SELECT 1
    FROM UNNEST(JSON_QUERY_ARRAY(e.data, '$.payload.message.mention.mentionees')) AS m
    WHERE JSON_VALUE(m, '$.isSelf') = 'true'
  ) AS mentions_bot,
  COALESCE(JSON_VALUE(e.data, '$.payload.deliveryContext.isRedelivery') = 'true', FALSE)
    AS is_redelivery,
  u.message_id IS NOT NULL AS unsent,
  e.publish_time
FROM `${PROJECT}.${DATASET}.v_events` AS e
LEFT JOIN unsent AS u
  ON u.message_id = JSON_VALUE(e.data, '$.payload.message.id')
WHERE e.kind = 'webhook' AND e.event_type = 'message'
