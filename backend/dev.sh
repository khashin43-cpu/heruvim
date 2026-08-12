export CORS_ALLOW_ORIGIN="${CORS_ALLOW_ORIGIN:-http://localhost:5173;http://localhost:8080;http://127.0.0.1:5173;http://127.0.0.1:8080}"
PORT="${PORT:-8080}"
uvicorn open_webui.main:app --port $PORT --host 0.0.0.0 --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-*}" --reload
