-- 重複排除済みの全イベント（Pub/Sub は少なくとも1回配信のため id で1件に絞る）
CREATE OR REPLACE VIEW `${PROJECT}.${DATASET}.v_events` AS
SELECT
  JSON_VALUE(data, '$.id') AS id,
  JSON_VALUE(data, '$.kind') AS kind,
  JSON_VALUE(data, '$.payload.type') AS event_type,
  SAFE.TIMESTAMP(JSON_VALUE(data, '$.received_at')) AS received_at,
  publish_time,
  data
FROM `${PROJECT}.${DATASET}.events_raw`
WHERE JSON_VALUE(data, '$.id') IS NOT NULL
QUALIFY ROW_NUMBER() OVER (PARTITION BY JSON_VALUE(data, '$.id') ORDER BY publish_time) = 1
