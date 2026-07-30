#!/usr/bin/env python3
"""Mock of api.collegefootballdata.com for end-to-end integration testing.
Serves the four endpoints the pipeline and site use, with fictional teams and
players in the real CFBD response shapes (v1 field names, plus the v2-style
variants the code tolerates). Run: python mock_cfbd.py [port]"""
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

TEAMS = [
    {"id": 1, "school": "Testville State", "conference": "Mock Conference A"},
    {"id": 2, "school": "Fakeburg Tech", "conference": "Mock Conference A"},
    {"id": 3, "school": "Sample U", "conference": "Mock Conference B"},
    {"id": 4, "school": "Placeholder College", "conference": "Mock Conference B"},
]

# player: (id, first, last, team, pos, class_int, h, w, city, st)
PLAYERS = [
    (9001, "Trip", "Testman", "Testville State", "QB", 3, 75, 214, "Testville", "TX"),
    (9002, "Rex", "Runner", "Testville State", "RB", 4, 70, 221, "Waco", "TX"),
    (9003, "Wes", "Wideout", "Testville State", "WR", 2, 73, 194, "Tulsa", "OK"),
    (9004, "Ty", "Endsley", "Fakeburg Tech", "TE", 3, 77, 248, "Ames", "IA"),
    (9005, "Quinn", "Quickly", "Fakeburg Tech", "QB", 2, 74, 205, "Reno", "NV"),
    (9006, "Sam", "Sampleton", "Sample U", "WR", 4, 71, 186, "Boise", "ID"),
    (9007, "Ath", "Letic", "Sample U", "ATH", 1, 72, 200, "Mesa", "AZ"),
    (9008, "Pete", "Placeholder", "Placeholder College", "RB", 3, 69, 208, "Provo", "UT"),
    (9009, "Zero", "Snapp", "Placeholder College", "QB", 1, 76, 218, "Ogden", "UT"),  # no stats ever
    (9010, "Trans", "Ferguson", "Sample U", "WR", 3, 72, 190, "Katy", "TX"),  # 2025 at Fakeburg Tech
]

def stat_rows(year):
    """CFBD /stats/player/season shape: one row per (player, category, statType)."""
    def rows(pid, name, team, conf, cat, kv):
        return [{"season": year, "playerId": pid, "player": name, "team": team,
                 "conference": conf, "category": cat, "statType": k, "stat": v}
                for k, v in kv.items()]
    out = []
    if year == 2026:
        out += rows(9001, "Trip Testman", "Testville State", "Mock Conference A",
                    "passing", {"COMPLETIONS": 118, "ATT": 172, "YDS": 1490, "TD": 14, "INT": 3})
        out += rows(9001, "Trip Testman", "Testville State", "Mock Conference A",
                    "rushing", {"CAR": 34, "YDS": 186, "TD": 3})
        out += rows(9002, "Rex Runner", "Testville State", "Mock Conference A",
                    "rushing", {"CAR": 98, "YDS": 612, "TD": 8})
        out += rows(9002, "Rex Runner", "Testville State", "Mock Conference A",
                    "receiving", {"REC": 11, "YDS": 84, "TD": 1})
        out += rows(9003, "Wes Wideout", "Testville State", "Mock Conference A",
                    "receiving", {"REC": 31, "YDS": 542, "TD": 6})
        out += rows(9005, "Quinn Quickly", "Fakeburg Tech", "Mock Conference A",
                    "passing", {"COMPLETIONS": 92, "ATT": 150, "YDS": 1210, "TD": 9, "INT": 5})
        out += rows(9010, "Trans Ferguson", "Sample U", "Mock Conference B",
                    "receiving", {"REC": 28, "YDS": 402, "TD": 3})
        out += rows(9008, "Pete Placeholder", "Placeholder College", "Mock Conference B",
                    "rushing", {"CAR": 74, "YDS": 388, "TD": 4})
        out += rows(9007, "Ath Letic", "Sample U", "Mock Conference B",
                    "receiving", {"REC": 9, "YDS": 121, "TD": 1})
    if year == 2025:
        out += rows(9001, "Trip Testman", "Testville State", "Mock Conference A",
                    "passing", {"COMPLETIONS": 240, "ATT": 371, "YDS": 3120, "TD": 26, "INT": 8})
        out += rows(9001, "Trip Testman", "Testville State", "Mock Conference A",
                    "rushing", {"CAR": 71, "YDS": 402, "TD": 6})
        out += rows(9002, "Rex Runner", "Testville State", "Mock Conference A",
                    "rushing", {"CAR": 231, "YDS": 1345, "TD": 15})
        out += rows(9004, "Ty Endsley", "Fakeburg Tech", "Mock Conference A",
                    "receiving", {"REC": 44, "YDS": 512, "TD": 5})
        out += rows(9006, "Sam Sampleton", "Sample U", "Mock Conference B",
                    "receiving", {"REC": 81, "YDS": 1104, "TD": 10})
        # transfer: 2025 stats at OLD school (Fakeburg Tech, Conf A) — tests
        # season-accurate school on a current Sample U player
        out += rows(9010, "Trans Ferguson", "Fakeburg Tech", "Mock Conference A",
                    "receiving", {"REC": 40, "YDS": 655, "TD": 5})
    return out

def games_players(year, team):
    """Minimal /games/players shape for games_played derivation."""
    games = []
    if year != 2026:
        return games
    counts = {"Testville State": {9001: 6, 9002: 6, 9003: 5},
              "Fakeburg Tech": {9005: 6, 9004: 4},
              "Sample U": {9006: 0, 9010: 5, 9007: 3},
              "Placeholder College": {9008: 6}}
    team_counts = counts.get(team, {})
    max_g = max(team_counts.values(), default=0)
    for g in range(max_g):
        aths = [{"id": pid, "name": "x", "stat": "1"}
                for pid, n in team_counts.items() if n > g]
        games.append({"id": year * 1000 + hash(team) % 100 + g,
                      "teams": [{"school": team, "categories": [
                          {"name": "receiving", "types": [
                              {"name": "REC", "athletes": aths}]}]}]})
    return games

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        year = int(q.get("year", 2026))
        if u.path == "/teams/fbs":
            body = TEAMS
        elif u.path == "/roster":
            team = q.get("team")
            body = [{"id": pid, "firstName": f, "lastName": l, "position": pos,
                     "year": cy, "height": h, "weight": w,
                     "homeCity": city, "homeState": st}
                    for (pid, f, l, t, pos, cy, h, w, city, st) in PLAYERS
                    if t == team]
        elif u.path == "/stats/player/season":
            conf = q.get("conference")
            body = [r for r in stat_rows(year)
                    if not conf or r["conference"] == conf]
        elif u.path == "/games/players":
            body = games_players(year, q.get("team", ""))
        else:
            self.send_response(404); self.end_headers(); return
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("access-control-allow-origin", "*")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def log_message(self, *a):  # quiet
        pass

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8199
    print(f"mock CFBD on :{port}")
    HTTPServer(("127.0.0.1", port), H).serve_forever()
