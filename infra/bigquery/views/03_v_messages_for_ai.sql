-- AI 用途はこのビューだけを使う（送信取消されたメッセージを除外）
CREATE OR REPLACE VIEW `${PROJECT}.${DATASET}.v_messages_for_ai` AS
SELECT * EXCEPT (unsent)
FROM `${PROJECT}.${DATASET}.v_messages`
WHERE NOT unsent
