def safe_query(inst, cmd: str, timeout_ms: int = 800) -> str | None:
    old = inst.timeout
    inst.timeout = timeout_ms
    try:
        return inst.query(cmd).strip()
    except Exception:
        return None
    finally:
        inst.timeout = old

ans = safe_query(sess.inst, "DISP:WIND1:TRAC:CAT?")
print("TRACE CAT:", ans)