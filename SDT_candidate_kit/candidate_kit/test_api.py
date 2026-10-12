import json
import urllib.request
import urllib.error

BASE_URL = "http://localhost:8000"

def request(method, path, data=None):
    url = f"{BASE_URL}{path}"
    req = urllib.request.Request(url, method=method)
    req.add_header("Content-Type", "application/json")
    body = json.dumps(data).encode("utf-8") if data else None
    try:
        with urllib.request.urlopen(req, data=body) as res:
            return res.getcode(), json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        content = e.read().decode("utf-8")
        try:
            return e.code, json.loads(content)
        except Exception:
            return e.code, content

def run_tests():
    results = []

    # 1. Health
    code, res = request("GET", "/health")
    results.append(("/health", code == 200, f"Code: {code}, res: {res}"))

    # 2. List Employees
    code, res = request("GET", "/employees?page=1&page_size=5")
    results.append(("/employees (list)", code == 200 and "items" in res, f"Code: {code}, total: {res.get('total') if isinstance(res, dict) else res}"))

    # 3. Create Employee
    new_emp = {
        "emp_code": "EMP9999",
        "name": "Test User",
        "email": "test@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01"
    }
    code, res = request("POST", "/employees", new_emp)
    results.append(("/employees (create)", code in (201, 409), f"Code: {code}, emp: {res.get('emp_code') if isinstance(res, dict) else res}"))

    # 4. List Attendance
    code, res = request("GET", "/attendance?page=1&page_size=5")
    results.append(("/attendance (list)", code == 200 and "items" in res, f"Code: {code}, total: {res.get('total') if isinstance(res, dict) else res}"))

    # 5. Punch-in
    # 2026-07-20 04:00:00 UTC = 09:30:00 IST -> epoch ms 1784520000000
    punch_in_payload = {
        "emp_code": "EMP9999",
        "punched_at": 1784520000000,
        "status": "PRESENT"
    }
    code, res = request("POST", "/attendance/punch-in", punch_in_payload)
    results.append(("/attendance/punch-in", code in (201, 409), f"Code: {code}, res: {res}"))

    # 6. Punch-out
    # 9 hours later = 1784552400000
    punch_out_payload = {
        "emp_code": "EMP9999",
        "punched_at": 1784552400000
    }
    code, res = request("POST", "/attendance/punch-out", punch_out_payload)
    results.append(("/attendance/punch-out", code in (200, 409), f"Code: {code}, res: {res}"))

    # 7. Regularize Attendance (PATCH)
    reg_payload = {
        "reason": "Adjustment approved by manager",
        "regularized_by": "manager.one",
        "punch_out": 1784556000000
    }
    code, res = request("PATCH", "/attendance/EMP9999/2026-07-20", reg_payload)
    results.append(("/attendance/{emp_code}/{date} (PATCH)", code in (200, 409), f"Code: {code}, res: {res}"))

    # 8. Employee Monthly Analytics
    code, res = request("GET", "/analytics/employees/EMP0001/monthly?month=2026-07")
    results.append(("/analytics/employees/{emp_code}/monthly", code == 200 and "working_days" in res, f"Code: {code}, res: {res}"))

    # 9. Department Summary Analytics
    code, res = request("GET", "/analytics/departments/summary?month=2026-07")
    results.append(("/analytics/departments/summary", code == 200 and "items" in res, f"Code: {code}, count: {len(res.get('items', [])) if isinstance(res, dict) else res}"))

    # 10. Late Leaderboard Analytics
    code, res = request("GET", "/analytics/leaderboard/late?month=2026-07&limit=5")
    results.append(("/analytics/leaderboard/late", code == 200 and "items" in res, f"Code: {code}, items: {res.get('items') if isinstance(res, dict) else res}"))

    # 11. Department Trend Analytics
    code, res = request("GET", "/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-07-10")
    results.append(("/analytics/departments/{dept}/trend", code == 200 and "items" in res, f"Code: {code}, items: {len(res.get('items', [])) if isinstance(res, dict) else res}"))

    # 12. Admin Explain
    for ep in ["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]:
        params = ""
        if ep == "employee_monthly":
            params = "?emp_code=EMP0001&month=2026-07"
        elif ep in ("department_summary", "late_leaderboard"):
            params = "?month=2026-07"
        elif ep == "department_trend":
            params = "?department=Engineering&from=2026-07-01&to=2026-07-10"

        code, res = request("GET", f"/admin/explain/{ep}{params}")
        results.append((f"/admin/explain/{ep}", code == 200 and "explain" in res, f"Code: {code}"))

    print("\n--- TEST REPORT ---")
    all_ok = True
    for endpoint, ok, detail in results:
        status_str = "PASS" if ok else "FAIL"
        if not ok:
            all_ok = False
        print(f"[{status_str}] {endpoint:<45} -> {detail}")
    print("-------------------")
    print(f"OVERALL: {'ALL ENDPOINTS WORKING PERFECTLY' if all_ok else 'SOME FAILED'}")

if __name__ == "__main__":
    run_tests()

