-- グループ・複数人トークごとの概況（名前は group_profile イベントの最新値）
CREATE OR REPLACE VIEW `${PROJECT}.${DATASET}.v_groups` AS
WITH ev AS (
  SELECT
    COALESCE(
      JSON_VALUE(data, '$.payload.source.groupId'),
      JSON_VALUE(data, '$.payload.source.roomId')
    ) AS group_id,
    JSON_VALUE(data, '$.payload.source.type') AS source_type,
    event_type,
    TIMESTAMP_MILLIS(SAFE_CAST(JSON_VALUE(data, '$.payload.timestamp') AS INT64)) AS event_at
  FROM `${PROJECT}.${DATASET}.v_events`
  WHERE kind = 'webhook'
    AND JSON_VALUE(data, '$.payload.source.type') IN ('group', 'room')
),
profile AS (
  SELECT
    JSON_VALUE(data, '$.payload.groupId') AS group_id,
    JSON_VALUE(data, '$.payload.groupName') AS group_name,
    JSON_VALUE(data, '$.payload.pictureUrl') AS picture_url,
    received_at AS profile_at
  FROM `${PROJECT}.${DATASET}.v_events`
  WHERE kind = 'group_profile'
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY JSON_VALUE(data, '$.payload.groupId') ORDER BY received_at DESC
  ) = 1
)
SELECT
  ev.group_id,
  ANY_VALUE(ev.source_type) AS source_type,
  ANY_VALUE(p.group_name) AS group_name,
  ANY_VALUE(p.picture_url) AS picture_url,
  MIN(IF(ev.event_type = 'join', ev.event_at, NULL)) AS joined_at,
  MAX(IF(ev.event_type = 'leave', ev.event_at, NULL)) AS left_at,
  MIN(ev.event_at) AS first_event_at,
  MAX(ev.event_at) AS last_event_at,
  COUNTIF(ev.event_type = 'message') AS messages
FROM ev
LEFT JOIN profile AS p USING (group_id)
GROUP BY ev.group_id
