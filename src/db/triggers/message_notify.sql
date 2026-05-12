-- 创建消息通知触发器函数
CREATE OR REPLACE FUNCTION notify_message_change()
RETURNS trigger AS $$
DECLARE
  payload JSON;
BEGIN
  IF (TG_OP = 'DELETE') THEN
    payload = json_build_object(
      'event', TG_OP,
      'session_id', OLD.session_id,
      'message_id', OLD.id,
      'sequence', OLD.sequence,
      'record_type', OLD.record_type,
      'data', row_to_json(OLD)
    );
  ELSE
    payload = json_build_object(
      'event', TG_OP,
      'session_id', NEW.session_id,
      'message_id', NEW.id,
      'sequence', NEW.sequence,
      'record_type', NEW.record_type,
      'data', row_to_json(NEW)
    );
  END IF;

  -- 发送PostgreSQL NOTIFY事件
  PERFORM pg_notify('message_channel', payload::text);

  RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql;

-- 创建触发器
DROP TRIGGER IF EXISTS message_trigger ON execution_records;
CREATE TRIGGER message_trigger
AFTER INSERT OR UPDATE OR DELETE ON execution_records
FOR EACH ROW
EXECUTE FUNCTION notify_message_change();