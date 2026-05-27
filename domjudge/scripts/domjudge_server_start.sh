#!/bin/bash
for c in domserver-{1,2,3,4} dj-mariadb-{1,2,3,4}; do
    podman stop "$c" 2>/dev/null
done

set -e

NETWORK_NAME="domjudge-net"

NUM_INSTANCES=4
DB_PORT_BASE=50034

DOMSERVER_PORT_BASE=50042

DB_IMAGE="docker.io/library/mariadb:latest"
DB_ROOT_PASSWORD="domjudge"
DB_USER="domjudge"
DB_PASSWORD="domjudge"
DB_NAME="domjudge"
DB_MAX_CONNECTIONS="1000"
DB_INNODB_BUFFER_POOL_SIZE="256M"

UPLOAD_LIMIT="512M"

DOMSERVER_IMAGE="docker.io/domjudge/domserver:latest"
TIMEZONE="UTC"
DOMJUDGE_HOST="${DOMJUDGE_HOST:-}"
if [ -z "$DOMJUDGE_HOST" ]; then
    DOMJUDGE_HOST=$(hostname -I 2>/dev/null | awk '{print $1; exit}' || hostname -f 2>/dev/null || echo "localhost")
fi

_db_container() { if [ "$NUM_INSTANCES" -le 1 ]; then echo "dj-mariadb"; else echo "dj-mariadb-$1"; fi; }
_dom_container() { if [ "$NUM_INSTANCES" -le 1 ]; then echo "domserver"; else echo "domserver-$1"; fi; }
_db_port() { if [ "$NUM_INSTANCES" -le 1 ]; then echo "50001"; else echo $((DB_PORT_BASE + $1)); fi; }
_dom_port() { if [ "$NUM_INSTANCES" -le 1 ]; then echo "50002"; else echo $((DOMSERVER_PORT_BASE + $1)); fi; }

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info()  { echo -e "${BLUE}[INFO]${NC} $1"; }
ok()    { echo -e "${GREEN}[OK]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
err()   { echo -e "${RED}[ERROR]${NC} $1"; }

if command -v docker &>/dev/null; then
    RUNTIME="docker"
elif command -v podman &>/dev/null; then
    RUNTIME="podman"
else
    err "Neither docker nor podman is installed."
    exit 1
fi
info "Container runtime: $RUNTIME"
if [ "$NUM_INSTANCES" -gt 4 ]; then
    max_proc=$(ulimit -u 2>/dev/null || echo "?")
    if [ "$max_proc" != "unlimited" ] && [ "${max_proc:-0}" -lt 4096 ] 2>/dev/null; then
        warn "NUM_INSTANCES=$NUM_INSTANCES may exhaust process limit (ulimit -u = $max_proc). Consider: ulimit -u 65535 or NUM_INSTANCES=4"
    fi
fi

create_network() {
    if $RUNTIME network inspect "$NETWORK_NAME" &>/dev/null; then
        ok "Network '$NETWORK_NAME' already exists"
        return 0
    fi
    info "Creating network '$NETWORK_NAME'..."
    local create_out create_ret
    create_out=$($RUNTIME network create "$NETWORK_NAME" 2>&1)
    create_ret=$?
    if [ "$create_ret" -eq 0 ]; then
        ok "Network created"
        return 0
    fi
    if echo "$create_out" | grep -qi "already exists"; then
        ok "Network '$NETWORK_NAME' already exists (reused)"
        return 0
    fi
    if $RUNTIME network inspect "$NETWORK_NAME" &>/dev/null; then
        ok "Network '$NETWORK_NAME' already exists (reused)"
        return 0
    fi
    err "Failed to create network '$NETWORK_NAME': $create_out"
    err "To remove and retry: $RUNTIME network rm $NETWORK_NAME"
    exit 1
}

start_db() {
    info "=== Starting MariaDB (${NUM_INSTANCES} instance(s)) ==="
    create_network

    local i
    for i in $(seq 1 $NUM_INSTANCES); do
        local cname dport
        cname=$(_db_container $i)
        dport=$(_db_port $i)
        if $RUNTIME inspect "$cname" &>/dev/null; then
            local state
            state=$($RUNTIME inspect --format '{{.State.Status}}' "$cname" 2>/dev/null)
            if [ "$state" = "running" ]; then
                ok "MariaDB [$i] already running ($cname port: $dport)"
                continue
            fi
        fi
        info "Creating and starting MariaDB container [$i] ($cname port: $dport)..."
        $RUNTIME run -d \
            --name "$cname" \
            --network "$NETWORK_NAME" \
            --log-opt max-size=50m \
            --log-opt max-file=3 \
            -e MYSQL_ROOT_PASSWORD="$DB_ROOT_PASSWORD" \
            -e MYSQL_USER="$DB_USER" \
            -e MYSQL_PASSWORD="$DB_PASSWORD" \
            -e MYSQL_DATABASE="$DB_NAME" \
            -p "${dport}:3306" \
            "$DB_IMAGE" \
            --max-connections="$DB_MAX_CONNECTIONS" \
            --max-allowed-packet="${UPLOAD_LIMIT}" \
            --innodb-buffer-pool-size="$DB_INNODB_BUFFER_POOL_SIZE" \
            --innodb_snapshot_isolation=OFF
        ok "MariaDB [$i] started (port: $dport)"
    done

    info "Waiting for MariaDB 'domjudge' database(s) to be ready..."
    for i in $(seq 1 $NUM_INSTANCES); do
        local cname
        cname=$(_db_container $i)
        for attempt in $(seq 1 45); do
            if $RUNTIME exec "$cname" mariadb -u"$DB_USER" -p"$DB_PASSWORD" -e "SHOW DATABASES LIKE '$DB_NAME';" 2>/dev/null | grep -q "$DB_NAME"; then
                ok "MariaDB [$i] ($cname) database fully ready!"
                break
            fi
            [ $attempt -eq 45 ] && warn "MariaDB [$i] init taking too long. Check '$RUNTIME logs $cname'."
            sleep 2
        done
    done
    sleep 3
}

start_domserver() {
    info "=== Starting domserver (${NUM_INSTANCES} instance(s)) ==="

    local i
    for i in $(seq 1 $NUM_INSTANCES); do
        local db_c dom_c dport
        db_c=$(_db_container $i)
        dom_c=$(_dom_container $i)
        dport=$(_dom_port $i)
        local db_state
        db_state=$($RUNTIME inspect --format '{{.State.Status}}' "$db_c" 2>/dev/null || echo "none")
        if [ "$db_state" != "running" ]; then
            warn "MariaDB [$i] ($db_c) is not running. Start DB first: $0 db"
            continue
        fi
        if $RUNTIME inspect "$dom_c" &>/dev/null; then
            local state
            state=$($RUNTIME inspect --format '{{.State.Status}}' "$dom_c" 2>/dev/null)
            if [ "$state" = "running" ]; then
                ok "domserver [$i] already running ($dom_c port: $dport)"
                continue
            fi
        fi
        info "Creating and starting domserver container [$i] ($dom_c port: $dport)..."
        $RUNTIME run -d \
            --name "$dom_c" \
            --network "$NETWORK_NAME" \
            --log-opt max-size=50m \
            --log-opt max-file=3 \
            -e CONTAINER_TIMEZONE="$TIMEZONE" \
            -e MYSQL_HOST="$db_c" \
            -e MYSQL_ROOT_PASSWORD="$DB_ROOT_PASSWORD" \
            -e MYSQL_USER="$DB_USER" \
            -e MYSQL_PASSWORD="$DB_PASSWORD" \
            -e MYSQL_DATABASE="$DB_NAME" \
            -e DJ_DB_INSTALL_EXAMPLES=0 \
            -p "${dport}:80" \
            "$DOMSERVER_IMAGE"
        ok "domserver [$i] started (port: $dport)"
    done

    info "Waiting for domserver web (Nginx) to be up (max 2 min per instance)..."
    for i in $(seq 1 $NUM_INSTANCES); do
        local dom_c dport
        dom_c=$(_dom_container $i)
        dport=$(_dom_port $i)
        for attempt in $(seq 1 60); do
            if curl -s -o /dev/null -w '%{http_code}' "http://localhost:${dport}/public" 2>/dev/null | grep -q '200\|301\|302'; then
                ok "domserver [$i] fully operational (port: $dport)"
                break
            fi
            [ $attempt -eq 60 ] && warn "domserver [$i] delayed. Check '$RUNTIME logs -f $dom_c'."
            sleep 2
        done
    done
    info "=== Applying upload limits (${UPLOAD_LIMIT}) to domserver instances ==="
    local FPM_CONF="/opt/domjudge/domserver/etc/domjudge-fpm.conf"
    for i in $(seq 1 $NUM_INSTANCES); do
        local dom_c
        dom_c=$(_dom_container $i)
        local state
        state=$($RUNTIME inspect --format '{{.State.Status}}' "$dom_c" 2>/dev/null || echo "none")
        [ "$state" != "running" ] && continue

        if $RUNTIME exec "$dom_c" test -f "$FPM_CONF" 2>/dev/null; then
            $RUNTIME exec "$dom_c" bash -c "
                conf='$FPM_CONF'; size='${UPLOAD_LIMIT}'
                if grep -q 'upload_max_filesize' \"\$conf\"; then
                    sed -i -E \"s|(php_admin_value\[upload_max_filesize\][[:space:]]*=).*|\1 \$size|\" \"\$conf\"
                else
                    echo \"php_admin_value[upload_max_filesize] = \$size\" >> \"\$conf\"
                fi
                if grep -q 'post_max_size' \"\$conf\"; then
                    sed -i -E \"s|(php_admin_value\[post_max_size\][[:space:]]*=).*|\1 \$size|\" \"\$conf\"
                else
                    echo \"php_admin_value[post_max_size] = \$size\" >> \"\$conf\"
                fi
            " 2>/dev/null && ok "[$i] $dom_c  PHP limits = ${UPLOAD_LIMIT}" \
                           || warn "[$i] $dom_c  PHP edit failed"
            $RUNTIME exec "$dom_c" supervisorctl restart php 2>/dev/null \
                && ok "[$i] $dom_c  PHP-FPM reloaded" \
                || warn "[$i] $dom_c  PHP-FPM reload failed"
        else
            warn "[$i] $dom_c  FPM conf not found — skip PHP limit"
        fi

        $RUNTIME exec "$dom_c" bash -c "
            size='${UPLOAD_LIMIT}'
            changed=0
            for f in \$(grep -rl 'client_max_body_size' /etc/nginx/ 2>/dev/null); do
                sed -i -E \"s|client_max_body_size[[:space:]]+[^;]+;|client_max_body_size \$size;|g\" \"\$f\"
                changed=1
            done
            if [ \"\$changed\" -eq 0 ]; then
                nginx_conf=/etc/nginx/nginx.conf
                if grep -q 'http {' \"\$nginx_conf\" 2>/dev/null; then
                    sed -i \"/http {/a\\\\    client_max_body_size \$size;\" \"\$nginx_conf\"
                else
                    echo \"client_max_body_size \$size;\" >> /etc/nginx/conf.d/domjudge.conf 2>/dev/null || true
                fi
            fi
        " 2>/dev/null && ok "[$i] $dom_c  Nginx client_max_body_size = ${UPLOAD_LIMIT}" \
                       || warn "[$i] $dom_c  Nginx edit failed"
        $RUNTIME exec "$dom_c" nginx -s reload 2>/dev/null \
            && ok "[$i] $dom_c  Nginx reloaded" \
            || warn "[$i] $dom_c  Nginx reload failed"
    done
    echo ""

    echo ""
    info "URL root for this host: ${DOMJUDGE_HOST} (set DOMJUDGE_HOST before running if different)"
    echo ""
    show_manual_password_guide
}

show_manual_password_guide() {
    info "=== Manual Password Setup Guide ==="
    local dom_c dport
    dom_c=$(_dom_container 1)
    dport=$(_dom_port 1)
    echo -e " 1. ${GREEN}Set web admin (admin) password:${NC}"
    echo -e "    $RUNTIME exec -it $dom_c /opt/domjudge/domserver/webapp/bin/console domjudge:reset-user-password admin"
    echo ""
    echo -e " 2. ${GREEN}Set judgehost API password:${NC}"
    echo -e "    $RUNTIME exec -it $dom_c /opt/domjudge/domserver/webapp/bin/console domjudge:reset-user-password judgehost"
    echo ""
    echo -e " 3. ${GREEN}Access URL(s) (DOMJUDGE_HOST=${DOMJUDGE_HOST}):${NC}"
    local i
    for i in $(seq 1 $NUM_INSTANCES); do
        echo -e "    Instance $i: http://${DOMJUDGE_HOST}:$( _dom_port $i )/"
    done
    if [ "$NUM_INSTANCES" -gt 1 ]; then
        echo -e "    ${YELLOW}Repeat steps 1–2 for each domserver (domserver-1 .. domserver-$((NUM_INSTANCES-1))).${NC}"
    fi
    echo ""
}

show_status() {
    info "=== DOMjudge Container Status (${NUM_INSTANCES} instance(s)) ==="
    local i
    for i in $(seq 1 $NUM_INSTANCES); do
        local db_c dom_c
        db_c=$(_db_container $i)
        dom_c=$(_dom_container $i)
        for container in "$db_c" "$dom_c"; do
            if $RUNTIME inspect "$container" &>/dev/null; then
                local state
                state=$($RUNTIME inspect --format '{{.State.Status}}' "$container" 2>/dev/null)
                if [ "$state" = "running" ]; then
                    echo -e "  ${GREEN}●${NC} $container: ${GREEN}$state${NC}"
                else
                    echo -e "  ${RED}●${NC} $container: ${RED}$state${NC}"
                fi
            else
                echo -e "  ${YELLOW}○${NC} $container: not found"
            fi
        done
    done
}

stop_all() {
    info "=== Stopping containers ==="
    local i
    for i in $(seq 1 $NUM_INSTANCES); do
        $RUNTIME stop "$(_dom_container $i)" "$(_db_container $i)" 2>/dev/null || true
    done
    ok "All containers stopped"
}

clean_all() {
    info "=== Full cleanup (including stale data) ==="
    warn "All data (including DB) will be permanently deleted! (executing in 5 seconds)"
    sleep 5

    local i
    for i in $(seq 1 $NUM_INSTANCES); do
        $RUNTIME rm -f -v "$(_dom_container $i)" "$(_db_container $i)" 2>/dev/null || true
    done
    $RUNTIME network rm "$NETWORK_NAME" 2>/dev/null || true

    ok "Full cleanup complete (including stale data)."
}

clear_queue() {
    info "=== Clearing judging queue (judgetask) for ${NUM_INSTANCES} instance(s) ==="
    local i
    for i in $(seq 1 $NUM_INSTANCES); do
        local cname
        cname=$(_db_container $i)
        local db_state
        db_state=$($RUNTIME inspect --format '{{.State.Status}}' "$cname" 2>/dev/null || echo "none")
        if [ "$db_state" != "running" ]; then
            warn "MariaDB [$i] ($cname) is not running; skip queue clear."
            continue
        fi
        local table_exists
        table_exists=$($RUNTIME exec "$cname" mariadb -u"$DB_USER" -p"$DB_PASSWORD" "$DB_NAME" -sN -e "SELECT 1 FROM information_schema.tables WHERE table_schema='$DB_NAME' AND table_name='judgetask' LIMIT 1;" 2>/dev/null || echo "")
        if [ "$table_exists" != "1" ]; then
            ok "[$i] No judgetask table; skip."
            continue
        fi
        local count
        count=$($RUNTIME exec "$cname" mariadb -u"$DB_USER" -p"$DB_PASSWORD" "$DB_NAME" -sN -e "SELECT COUNT(*) FROM judgetask;" 2>/dev/null || echo "?")
        if [ "${count:-0}" != "?" ] && [ "${count:-0}" -eq 0 ] 2>/dev/null; then
            ok "[$i] Queue already empty."
            continue
        fi
        $RUNTIME exec "$cname" mariadb -u"$DB_USER" -p"$DB_PASSWORD" "$DB_NAME" -e "DELETE FROM judgetask;" 2>/dev/null && ok "[$i] Judging queue cleared." || warn "[$i] DELETE judgetask failed."
    done
}

show_logs() {
    info "=== DOMjudge server logs (for 500 errors / debugging) ==="
    local dom_c db_c
    dom_c=$(_dom_container 1)
    db_c=$(_db_container 1)
    echo ""
    echo -e "  ${GREEN}1. Container stdout/stderr (instance 1):${NC}"
    echo -e "    $RUNTIME logs $dom_c"
    echo -e "    $RUNTIME logs -f $dom_c   # follow (live)"
    if [ "$NUM_INSTANCES" -gt 1 ]; then
        echo -e "    # Instance i: $RUNTIME logs $(_dom_container 1) ... $RUNTIME logs $(_dom_container $((NUM_INSTANCES-1)))"
    fi
    echo ""
    echo -e "  ${GREEN}2. Nginx error log (inside container):${NC}"
    echo -e "    $RUNTIME exec $dom_c tail -100 /var/log/nginx/error.log"
    echo ""
    echo -e "  ${GREEN}3. PHP-FPM / Symfony (if available):${NC}"
    echo -e "    $RUNTIME exec $dom_c tail -100 /var/log/php*-fpm.log 2>/dev/null || true"
    echo ""
    echo -e "  ${GREEN}4. MariaDB (instance 0):${NC}"
    echo -e "    $RUNTIME logs $db_c"
    echo ""
}

ACTION="${1:-all}"
case "$ACTION" in
    db) start_db ;;
    server) start_domserver ;;
    status) show_status ;;
    stop) stop_all ;;
    clean) clean_all ;;
    queue) clear_queue ;;
    logs) show_logs ;;
    all) start_db; echo ""; start_domserver ;;
    *) echo "Usage: $0 {all|db|server|status|stop|clean|queue|logs}"; exit 1 ;;
esac
