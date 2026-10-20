#!/usr/bin/env python3
"""
setup_dvwa_dc1_dc2.py

Sets up DVWA on dc1-h6 and dc2-h6 after the lab is deployed and the hosts are
configured. DC1 is the live site and DC2 the standby:

  1. waits for both MariaDB databases to accept connections
  2. creates a replication user on the DC1 database and points the DC2
     database at it, over the DCI (172.30.1.82 -> 172.30.2.82, port 3306)
  3. creates the DVWA tables on DC1 through DVWA's own setup page; the DC2
     database receives them by replication
  4. checks that replication is running and that both databases hold the
     same DVWA tables and users

The DC2 database is read-only, so DVWA in DC2 shows pages but cannot log in
until it is promoted. --promote makes DC2 the live site (stops replication and
makes the database writable), as a failover would.

Nothing is published on the lab host. To open DVWA in a browser from another
machine: ssh -L 8080:<management address printed below>:80 <lab host>, then
browse to http://localhost:8080 (user admin, password password).

USAGE (on the lab host, after configure_hosts_dc1_dc2.py):
  python3 setup_dvwa_dc1_dc2.py
  python3 setup_dvwa_dc1_dc2.py --promote     # fail DVWA over to DC2
"""

import re
import sys
import time
import argparse
import subprocess
import http.cookiejar
import urllib.parse
import urllib.request

PREFIX = "clab-DC1-DC2-"
PRIMARY, STANDBY = "dc1-h6", "dc2-h6"
PRIMARY_IP = "172.30.1.82"
ROOT_PW = "dvwa-root"
REPL_USER, REPL_PW = "repl", "repl-dvwa"


def sql(host, query):
    """Run SQL as root in a host's database container; return (rc, output)."""
    r = subprocess.run(
        ["docker", "exec", PREFIX + host + "-db", "mariadb", "-uroot", f"-p{ROOT_PW}",
         "-N", "-B", "-e", query],
        capture_output=True, text=True,
    )
    return r.returncode, (r.stdout + r.stderr).strip()


def mgmt_ip(host):
    r = subprocess.run(
        ["docker", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}",
         PREFIX + host], capture_output=True, text=True)
    return r.stdout.split()[0] if r.stdout.split() else None


def wait_for_db(host):
    for _ in range(60):
        rc, _ = sql(host, "SELECT 1")
        if rc == 0:
            return True
        time.sleep(2)
    return False


def dvwa_create_db(ip):
    """Press DVWA's "Create / Reset Database" button on setup.php."""
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    url = f"http://{ip}/setup.php"
    page = opener.open(url, timeout=15).read().decode(errors="replace")
    m = re.search(r"name='user_token' value='([0-9a-f]+)'", page) or \
        re.search(r'name="user_token" value="([0-9a-f]+)"', page)
    data = {"create_db": "Create / Reset Database"}
    if m:
        data["user_token"] = m.group(1)
    body = urllib.parse.urlencode(data).encode()
    return opener.open(url, data=body, timeout=30).read().decode(errors="replace")


def fail(msg):
    print(f"\nFAILED: {msg}")
    sys.exit(1)


def promote():
    print(f"Promoting {STANDBY}: stop replication, make the database writable")
    rc, out = sql(STANDBY, "STOP SLAVE; RESET SLAVE ALL; SET GLOBAL read_only=OFF;")
    if rc != 0:
        fail(out)
    print("Done. DVWA in DC2 now accepts logins and writes.")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--promote", action="store_true", help="make DC2 the live DVWA site")
    args = parser.parse_args()
    if args.promote:
        return promote()

    print("1. waiting for the databases")
    for h in (PRIMARY, STANDBY):
        if not wait_for_db(h):
            fail(f"{h}-db does not answer. Is the lab deployed with the DVWA nodes?")
        print(f"   {h}-db ready")

    print("2. replication DC1 -> DC2 over the DCI")
    rc, out = sql(PRIMARY,
                  f"CREATE USER IF NOT EXISTS '{REPL_USER}'@'172.30.%' IDENTIFIED BY '{REPL_PW}';"
                  f"GRANT REPLICATION SLAVE ON *.* TO '{REPL_USER}'@'172.30.%';")
    if rc != 0:
        fail(out)
    rc, out = sql(PRIMARY, "SHOW MASTER STATUS")
    if rc != 0 or not out:
        fail(f"no binary log on {PRIMARY}-db: {out}")
    log_file, log_pos = out.split()[0], out.split()[1]
    rc, out = sql(STANDBY,
                  "STOP SLAVE; RESET SLAVE ALL;"
                  f"CHANGE MASTER TO MASTER_HOST='{PRIMARY_IP}', MASTER_PORT=3306, "
                  f"MASTER_USER='{REPL_USER}', MASTER_PASSWORD='{REPL_PW}', "
                  f"MASTER_LOG_FILE='{log_file}', MASTER_LOG_POS={log_pos}, MASTER_USE_GTID=no, "
                  "MASTER_CONNECT_RETRY=5;"
                  "START SLAVE;")
    if rc != 0:
        fail(out)
    print(f"   {STANDBY}-db replicates from {PRIMARY_IP} ({log_file}:{log_pos})")

    print("3. creating the DVWA tables on DC1")
    ip = mgmt_ip(PRIMARY)
    if not ip:
        fail(f"no management address for {PRIMARY}")
    for _ in range(30):
        try:
            page = dvwa_create_db(ip)
            break
        except Exception as e:
            last = e
            time.sleep(2)
    else:
        fail(f"DVWA on {PRIMARY} did not answer at http://{ip}/: {last}")
    if "successfully" not in page.lower() and "setup successful" not in page.lower():
        print("   DVWA did not report success; check http://%s/setup.php" % ip)

    print("4. checking (waits up to 2 minutes for the DC2 database to connect over the DCI)")
    for _ in range(40):
        rc, status = sql(STANDBY, "SHOW SLAVE STATUS\\G")
        if "Slave_IO_Running: Yes" in status and "Slave_SQL_Running: Yes" in status:
            time.sleep(2)  # let it apply what DC1 has written
            break
        time.sleep(3)
    io = re.search(r"Slave_IO_Running: (\w+)", status)
    sq = re.search(r"Slave_SQL_Running: (\w+)", status)
    lag = re.search(r"Seconds_Behind_Master: (\w+)", status)
    err = re.search(r"Last_(?:IO_)?Error: (.+)", status)
    counts = {}
    for h in (PRIMARY, STANDBY):
        rc, out = sql(h, "SELECT COUNT(*) FROM dvwa.users; SELECT COUNT(*) FROM information_schema.tables "
                         "WHERE table_schema='dvwa';")
        counts[h] = out.split() if rc == 0 else [out]
        print(f"   {h}: dvwa users / tables = {' / '.join(counts[h])}")
    print(f"   replication IO {io and io.group(1)}, SQL {sq and sq.group(1)}, "
          f"seconds behind {lag and lag.group(1)}")
    print(f"\nDVWA (DC1): http://{ip}/   DVWA (DC2, read-only standby): http://{mgmt_ip(STANDBY)}/")
    print("From another machine: ssh -L 8080:<address above>:80 <lab host>, then http://localhost:8080")

    print("\n===== VERDICT =====")
    if io and sq and io.group(1) == "Yes" and sq.group(1) == "Yes" and counts[PRIMARY] == counts[STANDBY]:
        print("DVWA is up in DC1 and its database is replicated to DC2 over the DCI.")
    else:
        print("DVWA replication is NOT working.")
        if err and err.group(1).strip():
            print(f"  {err.group(1).strip()}")
        sys.exit(1)


if __name__ == "__main__":
    main()
