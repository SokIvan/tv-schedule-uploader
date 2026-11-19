"""Параллельное сканирование подсетей на TV."""
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed


def ping_one(ip: str, port: int, pin: str = "", timeout: float = 2.0):
    """Краткая проверка для сканирования. Возвращает {'ip','name'} или None."""
    url = f"http://{ip}:{port}/api/ping"
    headers = {"X-Pin": pin} if pin else {}
    try:
        r = requests.get(url, timeout=timeout, headers=headers)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except ValueError:
        return None
    if not data.get("ok"):
        return None
    return {"ip": ip, "name": data.get("name") or ip}


def check_one(ip: str, port: int, pin: str = "", timeout: float = 3.0) -> dict:
    """
    Детальная проверка одного IP для диагностики.
    Возвращает словарь:
      {"ip", "status": "ok"|"timeout"|"refused"|"pin_required"
                       |"invalid_json"|"http"|"not_ok"|"error",
       "name"?, "code"?, "detail"?, "data"?}
    """
    url = f"http://{ip}:{port}/api/ping"
    headers = {"X-Pin": pin} if pin else {}
    try:
        r = requests.get(url, timeout=timeout, headers=headers)
    except requests.Timeout:
        return {"ip": ip, "status": "timeout"}
    except requests.ConnectionError:
        return {"ip": ip, "status": "refused"}
    except requests.RequestException as e:
        return {"ip": ip, "status": "error", "detail": str(e)}

    if r.status_code == 401:
        return {"ip": ip, "status": "pin_required"}
    if r.status_code != 200:
        return {"ip": ip, "status": "http", "code": r.status_code}

    try:
        j = r.json()
    except ValueError:
        return {"ip": ip, "status": "invalid_json"}
    if not j.get("ok"):
        return {"ip": ip, "status": "not_ok", "data": j}
    return {"ip": ip, "status": "ok", "name": j.get("name") or ip}


def scan_network(subnets, port, pin="", progress_cb=None,
                 max_workers=40, timeout=1.5):
    """
    Сканировать список подсетей вида ["10.62.20", "10.62.21"].
    Возвращает СПИСОК только реально ответивших ТВ (не по одному на IP).
    """
    ips = [f"{s}.{i}" for s in subnets for i in range(1, 255)]
    total = len(ips)
    found = []
    done = 0

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(ping_one, ip, port, pin, timeout) for ip in ips]
        for fut in as_completed(futures):
            done += 1
            try:
                result = fut.result()
            except Exception:
                result = None
            if result:
                found.append(result)
            if progress_cb and (done % 20 == 0 or done == total):
                try:
                    progress_cb(done, total, len(found))
                except Exception:
                    pass
    return found