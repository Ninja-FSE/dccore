/* DCCore Dashboard - vanilla JS, no build step, no framework.
 *
 * Talks to the three original read-only endpoints (/api/search, /api/queue,
 * /api/filelists) plus the cross-bot search/fetch endpoints added alongside
 * them: POST /api/search/broadcast, GET /api/search/broadcast/status,
 * POST /api/fetch/enqueue, GET /api/fetch/status, GET /api/fetch/<id>/download,
 * POST /api/filelists/fetch, GET /api/filelists/bots, GET /api/filelists/bot/<nick>.
 * See webserver.py's module docstring - EVERY route requires a login,
 * including static assets, shared with the DCC CHAT admin console's
 * password. This file only ever runs after that login has already
 * succeeded (index.html itself is behind require_login()).
 */
(function () {
  "use strict";

  var REFRESH_MS = 8000;
  var BROADCAST_POLL_MS = 2000;
  var UPDATE_LIST_POLL_MS = 3000;
  var DOWNLOADS_POLL_MS = 4000;
  var FILELISTS_BOTS_POLL_MS = 4000;
  // Matches webserver.py's FILELISTS_DEFAULT_PAGE_SIZE - keep the two in
  // sync if either changes, so a page here always lines up with a page the
  // server actually hands back.
  var FILELISTS_PAGE_SIZE = 200;

  var views = {
    search:    { title: "Search",     sub: "Find a file across the current master list." },
    queue:     { title: "Queue",      sub: "Who is waiting, who is sending, right now." },
    download:  { title: "Download",   sub: "Bulk-paste \"!<bot> <filename>\" requests and track their progress." },
    filelists: { title: "File Lists", sub: "Every file this bot - or a fetched bot's list - is currently offering." },
    tools:     { title: "Tools",      sub: "Checks you run on demand against the current master list." },
    settings:  { title: "Settings",   sub: "Every editable setting, grouped. Saving writes settings.conf and starts a rehash." },
    stats:     { title: "Stats",      sub: "Everything this bot knows about itself." }
  };

  var state = {
    active: "search", filelistsLoaded: false, filelistsSource: "__own__",
    filelistsOffset: 0, filelistsTotal: 0, filelistsReturned: 0,
    filelistsHistory: [],
    settingsLoaded: false, settingsCategories: [], settingsActiveCategory: null,
    settingsBaseline: {}, settingsDirty: {}, settingsAdminPasswordSet: false
  };

  var el = {
    navItems:     document.querySelectorAll(".nav-item"),
    pageTitle:    document.getElementById("page-title"),
    pageSub:      document.getElementById("page-sub"),
    searchForm:   document.getElementById("search-form"),
    searchInput:  document.getElementById("search-input"),
    searchBody:   document.getElementById("search-body"),
    queueBody:    document.getElementById("queue-body"),
    filelistsBody:document.getElementById("filelists-body"),
    statSlots:    document.getElementById("stat-slots"),
    statFiles:    document.getElementById("stat-files"),
    statUsers:    document.getElementById("stat-users"),
    connDot:      document.getElementById("conn-dot"),
    connText:     document.getElementById("conn-text"),
    statusSlots:  document.getElementById("status-slots"),
    statusQueued: document.getElementById("status-queued"),
    broadcastBtn:        document.getElementById("broadcast-btn"),
    broadcastStatus:     document.getElementById("broadcast-status"),
    broadcastWrap:       document.getElementById("broadcast-wrap"),
    broadcastBody:       document.getElementById("broadcast-body"),
    downloadSelectedBtn: document.getElementById("download-selected-btn"),
    downloadsBody:       document.getElementById("downloads-body"),
    bulkFetchForm:       document.getElementById("bulk-fetch-form"),
    bulkFetchTextarea:   document.getElementById("bulk-fetch-textarea"),
    bulkFetchErrors:     document.getElementById("bulk-fetch-errors"),
    filelistsFetchForm:   document.getElementById("filelists-fetch-form"),
    filelistsFetchInput:  document.getElementById("filelists-fetch-input"),
    filelistsFetchStatus: document.getElementById("filelists-fetch-status"),
    filelistsSourceSelect: document.getElementById("filelists-source-select"),
    filelistsPrevBtn:     document.getElementById("filelists-prev-btn"),
    filelistsNextBtn:     document.getElementById("filelists-next-btn"),
    filelistsPageInfo:    document.getElementById("filelists-page-info"),
    filelistsExpandAll:   document.getElementById("filelists-expand-all"),
    filelistsCollapseAll: document.getElementById("filelists-collapse-all"),
    filelistsDownloadSelectedBtn: document.getElementById("filelists-download-selected-btn"),
    stSpeed:               document.getElementById("st-speed"),
    stRecord:              document.getElementById("st-record"),
    stSending:             document.getElementById("st-sending"),
    stQueued:              document.getElementById("st-queued"),
    stQueuedLabel:         document.getElementById("st-queued-label"),
    stUptime:              document.getElementById("st-uptime"),
    stSentTotal:           document.getElementById("st-sent-total"),
    stSentToday:           document.getElementById("st-sent-today"),
    stSentYesterday:       document.getElementById("st-sent-yesterday"),
    stSentTotalFiles:      document.getElementById("st-sent-total-files"),
    stSentTodayFiles:      document.getElementById("st-sent-today-files"),
    stSentYesterdayFiles:  document.getElementById("st-sent-yesterday-files"),
    stFiles:               document.getElementById("st-files"),
    stSize:                document.getElementById("st-size"),
    stAlbums:              document.getElementById("st-albums"),
    stBuilt:               document.getElementById("st-built"),
    stFoot:                document.getElementById("st-foot"),
    stTopFiles:            document.getElementById("st-top-files"),
    stTopAlbums:           document.getElementById("st-top-albums"),
    stTopAlbumsWrap:       document.getElementById("st-top-albums-wrap"),
    stTopAlbumsLabel:      document.getElementById("st-top-albums-label"),
    stTopAlbumsOff:        document.getElementById("st-top-albums-off"),
    themeDark:    document.getElementById("theme-dark"),
    themeLight:   document.getElementById("theme-light"),
    updateListRunBtn:     document.getElementById("update-list-run-btn"),
    updateListStatus:     document.getElementById("update-list-status"),
    verifyRunBtn:         document.getElementById("verify-run-btn"),
    verifyStatus:         document.getElementById("verify-status"),
    verifyResults:        document.getElementById("verify-results"),
    settingsRail:         document.getElementById("settings-rail"),
    settingsFields:       document.getElementById("settings-fields"),
    settingsSaveBtn:      document.getElementById("settings-save-btn"),
    settingsSavebarText:  document.getElementById("settings-savebar-text"),
    settingsRestartNote:  document.getElementById("settings-restart-note"),
    settingsSaveStatus:   document.getElementById("settings-save-error")
  };

  function escapeHtml(value) {
    var div = document.createElement("div");
    div.textContent = value === undefined || value === null ? "" : String(value);
    return div.innerHTML;
  }

  function emptyRow(colspan, text) {
    return '<tr class="empty-row"><td colspan="' + colspan + '">' + escapeHtml(text) + "</td></tr>";
  }

  function fetchJson(url) {
    return fetch(url, { headers: { Accept: "application/json" } }).then(function (res) {
      // A 401 is not a failed request, it is an expired session, and every
      // caller here turned it into "HTTP 401" and rendered the daemon as
      // unreachable. app.secret_key is os.urandom(32) per process, so EVERY
      // restart invalidates every cookie - a perfectly healthy bot then reads
      // as down, for ever, until somebody thinks to reload the page.
      //
      // The returned promise is deliberately left pending: navigation is
      // already under way, and resolving or rejecting it would flash an error
      // into a page that is about to be replaced.
      if (res.status === 401) {
        window.location.href = "/login";
        return new Promise(function () {});
      }
      if (!res.ok) { throw new Error("HTTP " + res.status); }
      return res.json();
    });
  }

  // postJson deliberately does NOT throw on a non-2xx response - the mutating
  // routes (broadcast/enqueue) return a meaningful JSON error body (409
  // already-in-progress, 429 cooldown, 503 IRC down, 400 bad input) that the
  // caller wants to show the operator, not treat as a network failure.
  function postJson(url, body) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(body)
    }).then(function (res) {
      return res.json().then(function (data) {
        return { ok: res.ok, status: res.status, data: data };
      });
    });
  }

  // -------------------------------------------------------------- Routing

  function activateView(name) {
    state.active = name;
    Object.keys(views).forEach(function (key) {
      document.getElementById("view-" + key).classList.toggle("is-active", key === name);
    });
    el.navItems.forEach(function (btn) {
      btn.classList.toggle("is-active", btn.dataset.view === name);
    });
    el.pageTitle.textContent = views[name].title;
    el.pageSub.textContent = views[name].sub;

    if (name === "queue") { loadQueue(); }
    if (name === "download") { loadDownloads(); }
    if (name === "filelists") {
      pollFilelistsBots();
      if (!state.filelistsLoaded) { loadFilelists(); }
    }
    if (name === "settings" && !state.settingsLoaded) { loadSettings(); }
    if (name === "stats") { loadStats(); }
  }

  el.navItems.forEach(function (btn) {
    btn.addEventListener("click", function () { activateView(btn.dataset.view); });
  });

  // --------------------------------------------------------------- Search

  el.searchForm.addEventListener("submit", function (evt) {
    evt.preventDefault();
    runSearch();
  });

  function runSearch() {
    var query = el.searchInput.value.trim();
    if (!query) {
      el.searchBody.innerHTML = emptyRow(4, "Type something and press Search.");
      return;
    }
    el.searchBody.innerHTML = emptyRow(4, "Searching…");
    fetchJson("/api/search?q=" + encodeURIComponent(query))
      .then(function (rows) {
        markConnection(true);
        if (!rows.length) {
          el.searchBody.innerHTML = emptyRow(4, "No matches for “" + query + "”.");
          return;
        }
        el.searchBody.innerHTML = rows.map(function (row) {
          return "<tr>" +
            "<td class=\"col-mono\">" + escapeHtml(row.title) + "</td>" +
            "<td class=\"col-dim col-mono\">" + escapeHtml(row.path) + "</td>" +
            "<td class=\"col-mono\">" + escapeHtml(row.size) + "</td>" +
            "<td class=\"col-dim\">" + escapeHtml(row.channel) + "</td>" +
            "</tr>";
        }).join("");
      })
      .catch(function (err) {
        markConnection(false);
        el.searchBody.innerHTML = emptyRow(4, "Search failed: " + err.message);
      });
  }

  // ------------------------------------------------------- Broadcast search

  var broadcast = { pollTimer: null, deadline: 0 };

  el.broadcastBtn.addEventListener("click", function () {
    var term = el.searchInput.value.trim();
    if (term.length < 3) {
      showBroadcastStatus("Type at least 3 characters first.", true);
      return;
    }
    el.broadcastBtn.disabled = true;
    postJson("/api/search/broadcast", { term: term }).then(function (res) {
      el.broadcastBtn.disabled = false;
      if (!res.ok) {
        showBroadcastStatus(res.data.error || ("HTTP " + res.status), true);
        return;
      }
      broadcast.deadline = res.data.deadline * 1000;
      el.broadcastWrap.style.display = "";
      el.broadcastBody.innerHTML = emptyRow(3, "Listening…");
      startBroadcastPolling();
    }).catch(function (err) {
      el.broadcastBtn.disabled = false;
      showBroadcastStatus("Request failed: " + err.message, true);
    });
  });

  function showBroadcastStatus(text, isError) {
    el.broadcastStatus.textContent = text;
    el.broadcastStatus.classList.toggle("is-error", !!isError);
  }

  function startBroadcastPolling() {
    if (broadcast.pollTimer) { clearInterval(broadcast.pollTimer); }
    pollBroadcastStatus();
    broadcast.pollTimer = setInterval(pollBroadcastStatus, BROADCAST_POLL_MS);
  }

  function pollBroadcastStatus() {
    fetchJson("/api/search/broadcast/status").then(function (payload) {
      markConnection(true);
      renderBroadcastResults(payload.results);
      if (payload.listening) {
        var remaining = Math.max(0, Math.ceil((payload.deadline * 1000 - Date.now()) / 1000));
        showBroadcastStatus("Listening… " + remaining + "s remaining, " +
          payload.results.length + " repl" + (payload.results.length === 1 ? "y" : "ies") + " so far.");
      } else {
        showBroadcastStatus(payload.results.length
          ? "Done. " + payload.results.length + " repl" + (payload.results.length === 1 ? "y" : "ies") + " received."
          : "Done. No replies received.");
        if (broadcast.pollTimer) {
          clearInterval(broadcast.pollTimer);
          broadcast.pollTimer = null;
        }
      }
    }).catch(function (err) {
      markConnection(false);
      showBroadcastStatus("Could not poll broadcast status: " + err.message, true);
    });
  }

  // Built via DOM APIs, not string-concatenated innerHTML, and this is load
  // bearing rather than a style choice: entry.bot/entry.filename come from
  // irc.py's best-effort "!<bot> <filename>" extraction against arbitrary
  // text ANY IRC user can PM/NOTICE to the bot during an open broadcast
  // window - i.e. attacker-controlled, unauthenticated text. Building the
  // bot/filename data attributes by hand from that text (even through
  // escapeHtml(), which never encodes the double-quote character) lets a
  // filename like: x[quote] autofocus onfocus=[quote]alert(1) - break out of
  // the attribute and inject a live event handler. Assigning through
  // .dataset instead has no such hazard: the browser sets the attribute
  // value directly, with no HTML parsing step for the attacker's text to
  // escape through. entry.from/entry.text are plain text-node content
  // (.textContent), which was never the vulnerable pattern - only
  // attribute-position values are.
  // Replies arrive interleaved - every bot's header, then its matches, in
  // whatever order they answer - which is unreadable once a dozen bots reply.
  // Grouped by sender instead: one heading per bot carrying what it told us
  // about itself, then its matches beneath it as the exact line you would
  // paste into the channel.
  function groupBroadcastResults(results) {
    var order = [];
    var groups = {};
    results.forEach(function (entry) {
      var who = entry.from || "?";
      if (!groups[who]) {
        groups[who] = { from: who, header: null, files: [], other: [] };
        order.push(who);
      }
      var g = groups[who];
      if (entry.header) {
        // Keep the first header. A bot that answers twice in one window is
        // reporting the same slot counts; the later one is not more true.
        g.header = g.header || entry.header;
      } else if (entry.bot && entry.filename) {
        g.files.push(entry);
      } else {
        g.other.push(entry);
      }
    });
    return order.map(function (who) { return groups[who]; })
                .sort(function (a, b) { return b.files.length - a.files.length; });
  }

  // What the bot said about itself, as one line. Every field is optional -
  // a missing one means "it did not say", never zero, so nothing is invented
  // to fill the gap.
  function describeBot(group) {
    var h = group.header || {};
    var bits = [];

    if (group.files.length) {
      // The header's count is what it FOUND; the rows are what it SENT.
      // Beezer finds 12 and sends 5, and saying only "5" would hide that
      // refining the search is worth doing.
      if (typeof h.matches === "number" && h.matches > group.files.length) {
        bits.push(h.matches + " matches, showing " + group.files.length);
      } else {
        bits.push(group.files.length + (group.files.length === 1 ? " match" : " matches"));
      }
    } else if (typeof h.matches === "number") {
      bits.push(h.matches + " matches, none sent");
    }

    if (typeof h.slots_free === "number" && typeof h.slots_total === "number") {
      bits.push(h.slots_free + "/" + h.slots_total + " slots free");
    } else if (typeof h.slots_in_use === "number") {
      // SPQR reports slots IN USE, the opposite sense - label it as such
      // rather than silently showing it where "free" would go.
      bits.push(h.slots_in_use + " slot" + (h.slots_in_use === 1 ? "" : "s") + " in use");
    }

    if (typeof h.queued === "number") {
      bits.push(typeof h.queue_total === "number"
        ? "queue " + h.queued + "/" + h.queue_total
        : h.queued + " queued");
    }

    if (typeof h.list_size === "number") {
      bits.push("list " + h.list_size.toLocaleString() + " files");
    }

    if (h.server) {
      bits.push(h.server);
    } else if (h.family === "spqr") {
      bits.push("SPQR");
    }

    return bits.join("  ·  ");
  }

  function renderBroadcastResults(results) {
    el.broadcastBody.innerHTML = "";
    if (!results.length) {
      el.broadcastBody.innerHTML = emptyRow(3, "No replies yet.");
      updateDownloadSelectedState();
      return;
    }

    groupBroadcastResults(results).forEach(function (group) {
      var headRow = document.createElement("tr");
      headRow.className = "bot-group";

      var headCell = document.createElement("td");
      headCell.colSpan = 3;

      var name = document.createElement("span");
      name.className = "bot-group-name";
      name.textContent = group.from;
      headCell.appendChild(name);

      var summary = describeBot(group);
      if (summary) {
        var meta = document.createElement("span");
        meta.className = "bot-group-meta";
        meta.textContent = summary;
        headCell.appendChild(meta);
      }

      headRow.appendChild(headCell);
      el.broadcastBody.appendChild(headRow);

      group.files.forEach(function (entry) {
        var tr = document.createElement("tr");

        var checkTd = document.createElement("td");
        checkTd.className = "col-check";
        var box = document.createElement("input");
        box.type = "checkbox";
        box.className = "broadcast-check";
        box.dataset.bot = entry.bot;
        box.dataset.filename = entry.filename;
        box.addEventListener("change", updateDownloadSelectedState);
        checkTd.appendChild(box);
        tr.appendChild(checkTd);

        // The exact line you would paste into the channel yourself, which is
        // also what the checkbox fetches - so what you read is what happens.
        var cmdTd = document.createElement("td");
        cmdTd.className = "col-mono";
        cmdTd.colSpan = 2;
        cmdTd.textContent = "!" + entry.bot + " " + entry.filename;
        tr.appendChild(cmdTd);

        el.broadcastBody.appendChild(tr);
      });

      // Anything that was neither a header nor a match still gets shown -
      // a bot saying it found nothing, or unrelated chatter that landed in
      // the window. Dimmed, and never with a checkbox, but never hidden:
      // silently dropping a reply would be worse than showing a stray line.
      group.other.forEach(function (entry) {
        var tr = document.createElement("tr");
        tr.appendChild(document.createElement("td"));
        var td = document.createElement("td");
        td.className = "col-dim col-mono";
        td.colSpan = 2;
        td.textContent = entry.text;
        tr.appendChild(td);
        el.broadcastBody.appendChild(tr);
      });
    });

    updateDownloadSelectedState();
  }

  function updateDownloadSelectedState() {
    var checked = el.broadcastBody.querySelectorAll(".broadcast-check:checked");
    el.downloadSelectedBtn.disabled = checked.length === 0;
  }

  el.downloadSelectedBtn.addEventListener("click", function () {
    var checked = el.broadcastBody.querySelectorAll(".broadcast-check:checked");
    var items = Array.prototype.map.call(checked, function (box) {
      return { bot: box.dataset.bot, filename: box.dataset.filename };
    });
    if (!items.length) { return; }
    el.downloadSelectedBtn.disabled = true;
    postJson("/api/fetch/enqueue", items).then(function (res) {
      if (!res.ok && !(res.data && res.data.created && res.data.created.length)) {
        showBroadcastStatus("Could not queue the download: " +
          (res.data.error || (res.data.errors && res.data.errors[0] && res.data.errors[0].error) || ("HTTP " + res.status)), true);
      } else {
        showBroadcastStatus("Queued " + res.data.created.length + " file(s) for fetch - see Queue → Downloads.");
        Array.prototype.forEach.call(checked, function (box) { box.checked = false; });
      }
      updateDownloadSelectedState();
      loadDownloads();
    }).catch(function (err) {
      showBroadcastStatus("Request failed: " + err.message, true);
      el.downloadSelectedBtn.disabled = false;
    });
  });

  // -------------------------------------------------------- Bulk fetch (Download tab)

  // Same tolerant pattern already used to build the Search view's broadcast
  // "Download" buttons under the hood (irc.py's own cross-bot capture uses
  // the equivalent on the IRC side) - reused here rather than reinvented, per
  // the brief: "!(\S+)\s+(.+)$".
  var BULK_FETCH_LINE_RE = /^!(\S+)\s+(.+)$/;

  el.bulkFetchForm.addEventListener("submit", function (evt) {
    evt.preventDefault();
    submitBulkFetch();
  });

  function submitBulkFetch() {
    var lines = el.bulkFetchTextarea.value.split(/\r?\n/);
    var items = [];
    var messages = [];

    lines.forEach(function (rawLine, idx) {
      var line = rawLine.trim();
      if (!line) { return; }
      var match = BULK_FETCH_LINE_RE.exec(line);
      if (!match) {
        messages.push({
          text: "Line " + (idx + 1) + ": could not parse “" + line +
                "” - expected \"!<bot> <filename>\".",
          isError: true
        });
        return;
      }
      items.push({ bot: match[1], filename: match[2] });
    });

    if (!items.length) {
      if (!messages.length) {
        messages.push({ text: "Nothing to queue - paste at least one \"!<bot> <filename>\" line.", isError: true });
      }
      renderBulkFetchMessages(messages);
      return;
    }

    postJson("/api/fetch/enqueue", items).then(function (res) {
      var createdCount = (res.data.created || []).length;
      (res.data.errors || []).forEach(function (err) {
        var item = err.item || {};
        messages.push({
          text: "\"!" + (item.bot != null ? item.bot : "?") + " " +
                (item.filename != null ? item.filename : "?") + "\": " + err.error,
          isError: true
        });
      });
      if (createdCount) {
        messages.unshift({ text: "Queued " + createdCount + " request(s) - see the Downloads table below.", isError: false });
        el.bulkFetchTextarea.value = "";
        loadDownloads();
      } else if (!messages.length) {
        messages.push({ text: res.data.error || "Nothing was queued.", isError: true });
      }
      renderBulkFetchMessages(messages);
    }).catch(function (err) {
      messages.push({ text: "Request failed: " + err.message, isError: true });
      renderBulkFetchMessages(messages);
    });
  }

  // Built via textContent, one <li> per message - never string-concatenated
  // innerHTML - even though this box is fed by the operator's own paste
  // rather than remote IRC text, the same "untrusted text never goes through
  // an HTML-parsing step" discipline applies everywhere else in this file.
  function renderBulkFetchMessages(messages) {
    el.bulkFetchErrors.innerHTML = "";
    if (!messages || !messages.length) { return; }
    var ul = document.createElement("ul");
    ul.className = "bulk-fetch-message-list";
    messages.forEach(function (msg) {
      var li = document.createElement("li");
      li.textContent = msg.text;
      li.className = msg.isError ? "is-error" : "is-ok";
      ul.appendChild(li);
    });
    el.bulkFetchErrors.appendChild(ul);
  }

  // -------------------------------------------------------------- Downloads

  // "rejected" is not a state dcc_fetch.py ever writes. A list archive whose
  // bytes arrived intact but which the extraction guard refused keeps
  // state === "complete", because the transfer really did succeed - the
  // reason it was refused is carried separately, in list_processing_error.
  // This is the display-side name for that combination.
  var DOWNLOAD_STATE_LABELS = {
    pending: "Pending", offered: "Offered", listening: "Listening",
    receiving: "Receiving", complete: "Complete", failed: "Failed",
    rejected: "Rejected"
  };

  function loadDownloads() {
    fetchJson("/api/fetch/status").then(function (rows) {
      markConnection(true);
      renderDownloads(rows);
    }).catch(function () { markConnection(false); });
  }

  // Delegated: renderDownloads() rebuilds the table's innerHTML on every
  // poll, which would silently drop a listener attached to any one row.
  el.downloadsBody.addEventListener("click", function (evt) {
    var btn = evt.target.closest ? evt.target.closest(".fetch-delete-btn") : null;
    if (!btn) { return; }
    var requestId = decodeURIComponent(btn.dataset.requestId);
    // A queued row has no file yet and can simply be re-queued, so the
    // finished-row warning would be both wrong and needlessly alarming.
    var prompt = btn.dataset.pending
      ? "Remove this queued request? Nothing has been downloaded yet."
      : "Delete this fetched file? This cannot be undone.";
    if (!window.confirm(prompt)) { return; }
    btn.disabled = true;
    postJson("/api/fetch/" + encodeURIComponent(requestId) + "/delete", {}).then(function (res) {
      if (!res.ok) {
        window.alert("Could not delete: " + (res.data && res.data.error || ("HTTP " + res.status)));
        btn.disabled = false;
        return;
      }
      loadDownloads();
    }).catch(function (err) {
      window.alert("Could not delete: " + err.message);
      btn.disabled = false;
    });
  });

  function renderDownloads(rows) {
    if (!rows.length) {
      el.downloadsBody.innerHTML = emptyRow(5, "Nothing queued yet.");
      return;
    }
    el.downloadsBody.innerHTML = rows.map(function (row) {
      var state = row.state || "pending";
      // dcc_fetch.py records a refused list archive on the row explicitly
      // "for the dashboard", and /api/fetch/status serves it - but nothing
      // here read it, so the field was served and discarded. A zip-slip
      // attempt from a foreign bot rendered as "Complete" with a working
      // Download button, indistinguishable from a list that fetched
      // perfectly, and the only record of the attempt was one stdout line.
      var rejected = !!row.list_processing_error;
      var displayState = rejected ? "rejected" : state;
      var label = DOWNLOAD_STATE_LABELS[displayState] || displayState;
      var progress = row.total_size
        ? Math.round(100 * (row.bytes_received || 0) / row.total_size) + "%"
        : (row.bytes_received ? row.bytes_received + " B" : "—");
      // Order matters: a rejected row is also state === "complete", so it has
      // to be tested first or it gets the Download button anyway.
      // A finished row (complete or failed) is deletable, and so is a pending
      // one: nothing has been dispatched for it yet, so there is nothing to
      // cancel - only a row to forget. The three genuinely in-flight states
      // (offered/listening/receiving) still get no button, because there is no
      // cancellation path for a transfer thread already running.
      // Without this the server-side fix would be invisible: the queue would
      // be clearable by API and not by the dashboard that filled it.
      var deletable = (state === "complete" || state === "failed" || state === "pending");
      // "Cancel" for a row that has not started - calling it Delete would
      // suggest a downloaded file is being thrown away when none exists.
      var deleteBtn = deletable
        ? "<button type=\"button\" class=\"btn btn-small btn-danger fetch-delete-btn\" data-request-id=\"" +
          encodeURIComponent(row.id) + "\" data-pending=\"" + (state === "pending" ? "1" : "") + "\">" +
          (state === "pending" ? "Cancel" : "Delete") + "</button>"
        : "";
      var action;
      if (rejected) {
        action = "<span class=\"col-dim\">" + escapeHtml(row.list_processing_error) + "</span> " + deleteBtn;
      } else if (state === "complete") {
        action = "<a class=\"btn btn-small\" href=\"/api/fetch/" + encodeURIComponent(row.id) + "/download\">Download</a> " + deleteBtn;
      } else if (state === "failed") {
        action = "<span class=\"col-dim\">" + escapeHtml(row.reason || "") + "</span> " + deleteBtn;
      } else if (state === "pending") {
        action = deleteBtn;
      } else {
        action = "";
      }
      // A "list" row (see dcc_fetch.py's request_type) starts with no
      // filename at all - we sent a bare "@<bot>" and cannot know what the
      // target bot will name its list zip until it actually answers. Show
      // something sensible instead of a blank cell until it does.
      var filenameDisplay = row.filename;
      if (!filenameDisplay && row.request_type === "list") {
        filenameDisplay = row.bot + "’s file list";
      } else if (row.request_type === "folder" && row.filename &&
                 row.filename === row.requested_filename) {
        // Not yet claimed: dcc_fetch.py's "folder" rows start with
        // filename === the literal "!rar <folder>" request text
        // (requested_filename, set once at creation) and only get the real
        // advertised .rar name once the other bot answers - show something
        // more readable than the raw wire command until then.
        filenameDisplay = row.filename.replace(/^!rar\s+/, "") + " (packing…)";
      }
      return "<tr>" +
        "<td class=\"col-mono\">" + escapeHtml(row.bot) + "</td>" +
        "<td class=\"col-dim col-mono\">" + escapeHtml(filenameDisplay) + "</td>" +
        "<td><span class=\"status-pill status-" + escapeHtml(displayState) + "\">" + escapeHtml(label) + "</span></td>" +
        "<td class=\"col-mono\">" + escapeHtml(progress) + "</td>" +
        "<td>" + action + "</td>" +
        "</tr>";
    }).join("");
  }

  // ---------------------------------------------------------------- Queue

  var STATUS_LABELS = { sending: "Sending", frozen: "Frozen", queued: "Queued", empty: "Empty" };

  function loadQueue() {
    fetchJson("/api/queue")
      .then(function (rows) {
        markConnection(true);
        renderQueueStats(rows);
        renderQueueTable(rows);
        renderSidebarStatus(rows);
      })
      .catch(function (err) {
        markConnection(false);
        el.queueBody.innerHTML = emptyRow(4, "Could not load the queue: " + err.message);
      });
  }

  function renderQueueTable(rows) {
    if (!rows.length) {
      el.queueBody.innerHTML = emptyRow(4, "The queue is empty.");
      return;
    }
    el.queueBody.innerHTML = rows.map(function (row) {
      var status = row.status || "queued";
      var label = STATUS_LABELS[status] || status;
      return "<tr>" +
        "<td class=\"col-mono\">" + escapeHtml(row.user) + "</td>" +
        "<td class=\"col-dim col-mono\">" + escapeHtml(row.preview) + "</td>" +
        "<td class=\"col-mono\">" + escapeHtml(row.count) + "</td>" +
        "<td><span class=\"status-pill status-" + escapeHtml(status) + "\">" + escapeHtml(label) + "</span></td>" +
        "</tr>";
    }).join("");
  }

  function renderQueueStats(rows) {
    var sending = rows.filter(function (r) { return r.status === "sending"; }).length;
    var totalFiles = rows.reduce(function (sum, r) { return sum + (r.count || 0); }, 0);
    el.statSlots.textContent = sending;
    el.statFiles.textContent = totalFiles;
    el.statUsers.textContent = rows.length;
  }

  function renderSidebarStatus(rows) {
    var sending = rows.filter(function (r) { return r.status === "sending"; }).length;
    var totalFiles = rows.reduce(function (sum, r) { return sum + (r.count || 0); }, 0);
    el.statusSlots.textContent = sending;
    el.statusQueued.textContent = totalFiles;
  }

  function markConnection(ok) {
    el.connDot.classList.toggle("is-live", ok);
    el.connDot.classList.toggle("is-down", !ok);
    el.connText.textContent = ok ? "live" : "unreachable";
  }

  // ------------------------------------------------------------ File Lists

  // Fetching another bot's list ------------------------------------------

  el.filelistsFetchForm.addEventListener("submit", function (evt) {
    evt.preventDefault();
    var bot = el.filelistsFetchInput.value.trim();
    if (!bot) { return; }
    postJson("/api/filelists/fetch", { bot: bot }).then(function (res) {
      if (!res.ok) {
        showFilelistsFetchStatus(res.data.error || ("HTTP " + res.status), true);
        return;
      }
      showFilelistsFetchStatus(
        "Requested " + bot + "’s list - track its progress on the Download tab; " +
        "it will appear in the switcher below once fetched.");
      el.filelistsFetchInput.value = "";
      pollFilelistsBots();
    }).catch(function (err) {
      showFilelistsFetchStatus("Request failed: " + err.message, true);
    });
  });

  function showFilelistsFetchStatus(text, isError) {
    el.filelistsFetchStatus.textContent = text;
    el.filelistsFetchStatus.classList.toggle("is-error", !!isError);
  }

  // Switching which bot's list is shown ------------------------------------

  el.filelistsSourceSelect.addEventListener("change", function () {
    state.filelistsSource = el.filelistsSourceSelect.value;
    state.filelistsOffset = 0;
    state.filelistsHistory = [];
    loadFilelists();
  });

  el.filelistsPrevBtn.addEventListener("click", function () {
    if (state.filelistsOffset <= 0) { return; }
    // Pop the offset this page was reached FROM rather than subtracting a
    // page size. Forward steps are not a fixed width (see Next below), so
    // arithmetic backwards would land somewhere no page ever started.
    state.filelistsOffset = state.filelistsHistory.length
      ? state.filelistsHistory.pop()
      : Math.max(0, state.filelistsOffset - FILELISTS_PAGE_SIZE);
    loadFilelists();
  });

  el.filelistsNextBtn.addEventListener("click", function () {
    // Advance by the number of folders the server actually RETURNED, not by
    // the number asked for. A page is also capped by a row ceiling, so a
    // request for 200 folders can come back with 33 - and stepping by 200
    // would skip the 167 in between without a word.
    var step = state.filelistsReturned || FILELISTS_PAGE_SIZE;
    var next = state.filelistsOffset + step;
    if (next >= state.filelistsTotal) { return; }
    state.filelistsHistory.push(state.filelistsOffset);
    state.filelistsOffset = next;
    loadFilelists();
  });

  function pollFilelistsBots() {
    fetchJson("/api/filelists/bots").then(function (rows) {
      markConnection(true);
      renderFilelistsSwitcher(rows);
    }).catch(function () { markConnection(false); });
  }

  function renderFilelistsSwitcher(rows) {
    var select = el.filelistsSourceSelect;
    var previous = state.filelistsSource || "__own__";

    select.innerHTML = "";
    var ownOption = document.createElement("option");
    ownOption.value = "__own__";
    ownOption.textContent = "Our own list";
    select.appendChild(ownOption);

    rows.forEach(function (row) {
      var opt = document.createElement("option");
      opt.value = row.bot;
      opt.textContent = row.bot + " (" + row.count + " files)";
      select.appendChild(opt);
    });

    var stillAvailable = previous === "__own__" ||
      rows.some(function (row) { return row.bot === previous; });
    select.value = stillAvailable ? previous : "__own__";
    state.filelistsSource = select.value;
  }

  // Loading the table itself ------------------------------------------------

    // Both /api/filelists and /api/filelists/bot/<nick> return a page of
    // FOLDERS - `{"folders","total","total_files","offset","limit"}` - rather
    // than a page of loose rows. Paging by the folder keeps a folder whole:
    // the folder is the unit an operator browses, and paging by row split
    // large ones across a page boundary, so page two opened mid-folder under
    // no heading at all.
    //
    // `total` counts folders, which is what the pager steps through;
    // `total_files` counts the rows inside them, which is what the operator
    // actually wants to know about the list.
    function renderFilelistsPager(totalFiles) {
      var total = state.filelistsTotal || 0;
      var offset = state.filelistsOffset || 0;
      var shown = state.filelistsReturned || 0;
      var start = shown === 0 ? 0 : offset + 1;
      var end = offset + shown;
      var files = totalFiles || 0;
      el.filelistsPageInfo.textContent =
        "Folders " + start.toLocaleString() + "–" + end.toLocaleString() +
        " of " + total.toLocaleString() +
        " (" + files.toLocaleString() + (files === 1 ? " file)" : " files)");
      el.filelistsPrevBtn.disabled = offset <= 0;
      el.filelistsNextBtn.disabled = (offset + shown) >= total;
    }

    // A file that reached us with no folder heading above it still has to be
    // shown under something.
    function folderLabel(name) {
      return name ? name : "(no folder)";
    }

    // The count is the point of a collapsed folder: it says how much is inside
    // before the operator spends a click finding out. `count` is the folder's
    // TRUE size, so a folder that arrived truncated still reports what it
    // holds rather than only what fitted on the page.
    function folderHeadingHtml(group, index) {
      var count = group.count || 0;
      // Packing a whole folder as .rar only makes sense against another
      // bot's list - browsing our own is direct filesystem access already,
      // same gate folderFilesHtml() already applies to the per-file
      // checkbox column.
      var fetchable = (state.filelistsSource || "__own__") !== "__own__";
      // data-folder-index is safe to string-concatenate: it is this group's
      // own position in the internal `groups` array (an internal loop
      // index), not untrusted content - unlike the bot/folder values
      // attachFilelistsFolderRarData() sets below via .dataset assignment.
      var rarButton = fetchable
        ? "<button type=\"button\" class=\"btn btn-small folder-rar-btn\"" +
          " data-folder-index=\"" + index + "\">Get folder as .rar</button>"
        : "";
      return "<tr class=\"folder-row\">" +
        "<td colspan=\"5\">" +
          "<button type=\"button\" class=\"folder-toggle\" aria-expanded=\"false\"" +
                 " data-folder-index=\"" + index + "\">" +
            "<span class=\"folder-caret\" aria-hidden=\"true\"></span>" +
            "<span class=\"folder-name\">" +
              escapeHtml(folderLabel(group.folder)) + "</span>" +
            "<span class=\"folder-count\">" + count.toLocaleString() +
              (count === 1 ? " file" : " files") + "</span>" +
          "</button>" +
          rarButton +
        "</td></tr>";
    }

    function folderFilesHtml(group, index) {
      var entries = group.entries || [];
      // A file is only fetchable when it belongs to someone ELSE's list -
      // browsing our own is direct filesystem access already, and
      // /api/fetch/enqueue exists to reach another bot over IRC, not this one.
      var fetchable = (state.filelistsSource || "__own__") !== "__own__";
      var rows = entries.map(function (row) {
        // No data-bot/data-filename attribute here, and no bot/filename text
        // anywhere in this markup fragment: `row.source`/`row.title` come
        // from another bot's fetched list file - attacker-controlled the
        // same way the broadcast table's entry.bot/entry.filename were (see
        // BroadcastRenderingXssRegressionTests) - and escapeHtml() does not
        // encode `"`, so string-concatenating either into an HTML attribute
        // value is exactly BUG 2 again. attachFilelistsCheckboxData(), called
        // right after this HTML lands in the DOM, sets both via .dataset
        // instead, which the browser assigns as a property with no
        // HTML-parsing step for the value to break out through.
        var checkCell = fetchable
          ? "<td class=\"col-check\"><input type=\"checkbox\" class=\"filelists-check\"></td>"
          : "<td class=\"col-check\"></td>";
        return "<tr class=\"file-row is-hidden\" data-folder-index=\"" + index + "\">" +
          checkCell +
          "<td class=\"col-mono col-indent\">" + escapeHtml(row.title) + "</td>" +
          "<td class=\"col-mono\">" + escapeHtml(row.size) + "</td>" +
          "<td class=\"col-dim\">" + escapeHtml(row.format) + "</td>" +
          "<td class=\"col-dim col-mono\">" + escapeHtml(row.source) + "</td>" +
          "</tr>";
      });
      // A folder bigger than the page's row ceiling arrives cut short. Say so
      // in place, so the rows on screen cannot quietly disagree with the count
      // in the heading right above them.
      if (group.truncated) {
        rows.push(
          "<tr class=\"file-row folder-truncated is-hidden\" data-folder-index=\"" +
          index + "\"><td colspan=\"5\">Showing the first " +
          entries.length.toLocaleString() + " of " +
          (group.count || 0).toLocaleString() +
          " files in this folder.</td></tr>");
      }
      return rows.join("");
    }

    function setFolderExpanded(index, expanded) {
      var rows = el.filelistsBody.querySelectorAll(
        ".file-row[data-folder-index=\"" + index + "\"]");
      for (var i = 0; i < rows.length; i++) {
        rows[i].classList.toggle("is-hidden", !expanded);
      }
    }

    function setAllFolders(expanded) {
      var toggles = el.filelistsBody.querySelectorAll(".folder-toggle");
      for (var i = 0; i < toggles.length; i++) {
        toggles[i].setAttribute("aria-expanded", expanded ? "true" : "false");
      }
      var rows = el.filelistsBody.querySelectorAll(".file-row");
      for (var j = 0; j < rows.length; j++) {
        rows[j].classList.toggle("is-hidden", !expanded);
      }
    }

    // Delegated once here rather than bound per heading: the table is rebuilt
    // on every page change and every source change, and a per-heading listener
    // would have to be re-attached each time.
    el.filelistsBody.addEventListener("click", function (evt) {
      var toggle = evt.target.closest ? evt.target.closest(".folder-toggle") : null;
      if (toggle) {
        var expanded = toggle.getAttribute("aria-expanded") === "true";
        toggle.setAttribute("aria-expanded", expanded ? "false" : "true");
        setFolderExpanded(toggle.getAttribute("data-folder-index"), !expanded);
        return;
      }
      var rarBtn = evt.target.closest ? evt.target.closest(".folder-rar-btn") : null;
      if (rarBtn) { requestFolderRar(rarBtn); }
    });

    el.filelistsExpandAll.addEventListener("click", function () { setAllFolders(true); });
    el.filelistsCollapseAll.addEventListener("click", function () { setAllFolders(false); });

    // Delegated for the same reason as the folder-toggle listener above: the
    // checkboxes are rebuilt from scratch on every page/source change, so a
    // listener attached per-checkbox would need re-attaching every time.
    el.filelistsBody.addEventListener("change", function (evt) {
      if (evt.target.classList && evt.target.classList.contains("filelists-check")) {
        updateFilelistsDownloadSelectedState();
      }
    });

    function updateFilelistsDownloadSelectedState() {
      var checked = el.filelistsBody.querySelectorAll(".filelists-check:checked");
      el.filelistsDownloadSelectedBtn.disabled = checked.length === 0;
    }

    // Sets each checkbox's bot/filename via .dataset - a property assignment
    // the browser stores as-is, with no HTML-attribute-parsing step for
    // either value to break out through - rather than string-concatenating
    // them into the markup folderFilesHtml() already built (see that
    // function's comment, and BroadcastRenderingXssRegressionTests, for why
    // that would reopen BUG 2). Walks `groups` in the exact order
    // folderFilesHtml() rendered it in, so the Nth checkbox in the DOM lines
    // up with the Nth fetchable row here - true because every row in one
    // render shares the same `fetchable` value, so a render either produces
    // zero checkboxes or exactly one per row, never a mix to line up against.
    function attachFilelistsCheckboxData(groups) {
      var boxes = el.filelistsBody.querySelectorAll(".filelists-check");
      if (!boxes.length) { return; }
      var i = 0;
      groups.forEach(function (group) {
        (group.entries || []).forEach(function (row) {
          if (i >= boxes.length) { return; }
          boxes[i].dataset.bot = row.source;
          boxes[i].dataset.filename = row.title;
          i += 1;
        });
      });
    }

    // Same reasoning and same pattern as attachFilelistsCheckboxData() right
    // above: state.filelistsSource/group.folder must NEVER be
    // string-concatenated into an HTML attribute (escapeHtml() does not
    // encode `"`, so a malicious folder name could break out of one - see
    // BroadcastRenderingXssRegressionTests for the bug class this avoids).
    // Each button already carries its own group's index via
    // data-folder-index (safe - an internal loop index, set in
    // folderHeadingHtml() above), so this just looks that index back up in
    // `groups` and sets the real bot/folder via .dataset property
    // assignment instead.
    function attachFilelistsFolderRarData(groups) {
      var buttons = el.filelistsBody.querySelectorAll(".folder-rar-btn");
      for (var i = 0; i < buttons.length; i++) {
        var button = buttons[i];
        var index = parseInt(button.dataset.folderIndex, 10);
        var group = groups[index];
        if (!group) { continue; }
        button.dataset.bot = state.filelistsSource;
        button.dataset.folder = group.folder;
      }
    }

    function requestFolderRar(button) {
      var bot = button.dataset.bot;
      var folder = button.dataset.folder;
      if (!bot || !folder) { return; }
      button.disabled = true;
      postJson("/api/filelists/fetch-folder-rar", { bot: bot, folder: folder }).then(function (res) {
        if (!res.ok) {
          showFilelistsFetchStatus(res.data.error || ("HTTP " + res.status), true);
          button.disabled = false;
          return;
        }
        showFilelistsFetchStatus(
          "Requested " + bot + "’s folder as .rar - track its progress on the Download tab.");
        loadDownloads();
      }).catch(function (err) {
        showFilelistsFetchStatus("Request failed: " + err.message, true);
        button.disabled = false;
      });
    }

    el.filelistsDownloadSelectedBtn.addEventListener("click", function () {
      var checked = el.filelistsBody.querySelectorAll(".filelists-check:checked");
      var items = Array.prototype.map.call(checked, function (box) {
        return { bot: box.dataset.bot, filename: box.dataset.filename };
      });
      if (!items.length) { return; }
      el.filelistsDownloadSelectedBtn.disabled = true;
      postJson("/api/fetch/enqueue", items).then(function (res) {
        if (!res.ok && !(res.data && res.data.created && res.data.created.length)) {
          showFilelistsFetchStatus("Could not queue the download: " +
            (res.data.error || (res.data.errors && res.data.errors[0] && res.data.errors[0].error) || ("HTTP " + res.status)), true);
        } else {
          showFilelistsFetchStatus(
            "Queued " + res.data.created.length + " file(s) for fetch - see Queue → Downloads.", false);
          Array.prototype.forEach.call(checked, function (box) { box.checked = false; });
        }
        updateFilelistsDownloadSelectedState();
        loadDownloads();
      }).catch(function (err) {
        showFilelistsFetchStatus("Could not queue the download: " + err.message, true);
        updateFilelistsDownloadSelectedState();
      });
    });

    // Accepts the older flat shapes as well as the folder one: a bare array,
    // or a payload carrying `entries`, both become a single unnamed folder
    // rather than a blank table. That is the same defensive unwrap this did
    // before paging existed, extended to cover the shape that replaced it.
    function folderGroupsFrom(payload) {
      if (Array.isArray(payload)) {
        return payload.length
          ? [{ folder: "", count: payload.length, entries: payload }]
          : [];
      }
      if (payload.folders) { return payload.folders; }
      var rows = payload.entries || [];
      return rows.length ? [{ folder: "", count: rows.length, entries: rows }] : [];
    }

    function loadFilelists() {
      el.filelistsBody.innerHTML = emptyRow(5, "Loading…");
      var source = state.filelistsSource || "__own__";
      var offset = state.filelistsOffset || 0;
      var base = (source === "__own__")
        ? "/api/filelists"
        : "/api/filelists/bot/" + encodeURIComponent(source);
      var url = base + "?offset=" + offset + "&limit=" + FILELISTS_PAGE_SIZE;

      fetchJson(url)
        .then(function (payload) {
          markConnection(true);
          state.filelistsLoaded = true;
          var groups = folderGroupsFrom(payload);
          state.filelistsTotal = Array.isArray(payload)
            ? groups.length : (payload.total || 0);
          state.filelistsReturned = groups.length;
          renderFilelistsPager(Array.isArray(payload)
            ? payload.length : (payload.total_files || 0));
          if (!groups.length) {
            el.filelistsBody.innerHTML = emptyRow(5, "No files published yet.");
            updateFilelistsDownloadSelectedState();
            return;
          }
          el.filelistsBody.innerHTML = groups.map(function (group, index) {
            return folderHeadingHtml(group, index) + folderFilesHtml(group, index);
          }).join("");
          attachFilelistsCheckboxData(groups);
          attachFilelistsFolderRarData(groups);
          updateFilelistsDownloadSelectedState();
        })
        .catch(function (err) {
          markConnection(false);
          el.filelistsBody.innerHTML = emptyRow(5, "Could not load file lists: " + err.message);
          updateFilelistsDownloadSelectedState();
        });
    }

  // ---------------------------------------------------------------- Tools

  // Rebuilding the list is the dashboard's own equivalent of !update -
  // added because FILE_DIRECTORY is deliberately not in
  // settings_file.REQUIRED (see settings_file.py's own comment): an
  // operator who sets it for the first time from the Settings page had no
  // way at all to then build the list it enables, short of a real IRC
  // client or a CLI already running. A rebuild can take minutes on a real
  // library, so this polls /status rather than waiting on the POST itself -
  // the same shape as broadcast search above.
  var updateList = { pollTimer: null };

  el.updateListRunBtn.addEventListener("click", function () {
    el.updateListRunBtn.disabled = true;
    showUpdateListStatus("Starting…", false);
    postJson("/api/tools/update-list", {}).then(function (res) {
      if (!res.ok) {
        el.updateListRunBtn.disabled = false;
        showUpdateListStatus(res.data.error || ("HTTP " + res.status), true);
        return;
      }
      startUpdateListPolling();
    }).catch(function (err) {
      el.updateListRunBtn.disabled = false;
      showUpdateListStatus("Request failed: " + err.message, true);
    });
  });

  function showUpdateListStatus(text, isError) {
    el.updateListStatus.textContent = text;
    el.updateListStatus.classList.toggle("is-error", !!isError);
  }

  function startUpdateListPolling() {
    if (updateList.pollTimer) { clearInterval(updateList.pollTimer); }
    pollUpdateListStatus();
    updateList.pollTimer = setInterval(pollUpdateListStatus, UPDATE_LIST_POLL_MS);
  }

  function pollUpdateListStatus() {
    fetchJson("/api/tools/update-list/status").then(function (payload) {
      markConnection(true);
      if (payload.running) {
        showUpdateListStatus("Rebuilding the master list…", false);
        return;
      }
      clearInterval(updateList.pollTimer);
      updateList.pollTimer = null;
      el.updateListRunBtn.disabled = false;
      // #224: "running" alone cannot tell a rebuild that worked from one
      // that failed - this used to say "Done" unconditionally the moment
      // running flipped false, whichever it was.
      if (payload.ok === false) {
        showUpdateListStatus("Failed: " + (payload.error || "unknown error"), true);
      } else {
        showUpdateListStatus("Done. Check Stats for the new file count.", false);
      }
    }).catch(function (err) {
      markConnection(false);
      clearInterval(updateList.pollTimer);
      updateList.pollTimer = null;
      el.updateListRunBtn.disabled = false;
      showUpdateListStatus("Lost track of the update: " + err.message, true);
    });
  }

  // The Tools view runs nothing on its own beyond the update above.
  // Verifying the list re-reads and re-parses the whole master list, which
  // is work worth doing when the operator asks for it and not on every tab
  // switch.
  function renderVerifyResults(payload) {
    var duplicates = payload.duplicates || [];
    var checked = payload.checked || 0;

    if (!duplicates.length) {
      el.verifyStatus.textContent =
        "No duplicates. All " + checked.toLocaleString() +
        " filenames in the list are unique.";
      el.verifyStatus.classList.remove("is-error");
      el.verifyResults.innerHTML = "";
      return;
    }

    // "shadowed", not "unreachable" - see webserver.build_verify_list_payload().
    // Since #128 a requester pasting a search result's whole line reaches the
    // copy its size names; it is the bare-name request that only ever gets the
    // first one listed.
    var shadowed = payload.shadowed || 0;
    el.verifyStatus.textContent =
      duplicates.length.toLocaleString() +
      (duplicates.length === 1 ? " filename appears" : " filenames appear") +
      " under more than one folder, out of " + checked.toLocaleString() +
      " checked. " + shadowed.toLocaleString() +
      (shadowed === 1 ? " copy is" : " copies are") +
      " reachable only by pasting a search result's whole line.";
    el.verifyStatus.classList.remove("is-error");

    el.verifyResults.innerHTML = duplicates.map(function (item) {
      // The FIRST folder is the one a request for this name actually
      // reaches - dcc.py resolves the name against this same list and takes
      // the first match. Marking it is the whole point of showing the
      // folders in list order rather than sorted.
      var folders = (item.folders || []).map(function (folder, index) {
        return "<li class=\"verify-folder" + (index === 0 ? " is-served" : "") + "\">" +
          "<span class=\"verify-path\">" +
            escapeHtml(folder || "(library root)") + "</span>" +
          (index === 0 ? "<span class=\"verify-tag\">served</span>"
                       : "<span class=\"verify-tag is-dim\">shadowed</span>") +
          "</li>";
      }).join("");
      return "<div class=\"verify-group\">" +
        "<div class=\"verify-name\">" +
          "<span class=\"verify-file\">" + escapeHtml(item.filename) + "</span>" +
          "<span class=\"verify-count\">" + (item.count || 0) + " folders</span>" +
        "</div>" +
        "<ul class=\"verify-folders\">" + folders + "</ul>" +
      "</div>";
    }).join("");
  }

  el.verifyRunBtn.addEventListener("click", function () {
    el.verifyRunBtn.disabled = true;
    el.verifyStatus.classList.remove("is-error");
    el.verifyStatus.textContent = "Reading the master list…";
    el.verifyResults.innerHTML = "";

    fetchJson("/api/tools/verify-list")
      .then(function (payload) {
        markConnection(true);
        renderVerifyResults(payload);
      })
      .catch(function (err) {
        markConnection(false);
        el.verifyStatus.textContent = "Could not verify the list: " + err.message;
        el.verifyStatus.classList.add("is-error");
      })
      .then(function () {
        el.verifyRunBtn.disabled = false;
      });
  });

  // ------------------------------------------------------------- Settings

  // Values arrive from GET /api/settings already in their real Python types
  // (bool/int/float/list/str/null); every value POSTed back must be a
  // string (settings_file.save() coerces it the same way settings.conf
  // itself would be read) - this turns a loaded value, or nothing at all if
  // the field is still dirty, into the string a fresh baseline comparison
  // needs.
  function settingsValueToString(value) {
    if (typeof value === "boolean") { return value ? "true" : "false"; }
    if (Array.isArray(value)) { return value.join(", "); }
    if (value === null || value === undefined) { return ""; }
    return String(value);
  }

  function settingsFieldHtml(field) {
    var isDirty = Object.prototype.hasOwnProperty.call(state.settingsDirty, field.name);
    var nameClass = "settings-field-name" + (isDirty ? " is-dirty" : "");
    var control;

    if (field.choices) {
      // A fixed few, not free text. The three list formats are the first: a
      // typed "ZIP" or "tar" would be refused by the save with a reason, but
      // being refused is a worse way to find out than never being offered it.
      var options = field.choices.map(function (choice) {
        return '<option value="' + escapeHtml(choice) + '">' + escapeHtml(choice) + "</option>";
      }).join("");
      control = '<select data-setting="' + escapeHtml(field.name) + '">' + options + "</select>";
    } else if (field.type === "bool") {
      var checked = isDirty ? state.settingsDirty[field.name] === "true" : !!field.value;
      control = '<input type="checkbox" data-setting="' + escapeHtml(field.name) + '"' +
        (checked ? " checked" : "") + ">";
    } else if (field.type === "int" || field.type === "float") {
      control = '<input type="number" step="' + (field.type === "float" ? "any" : "1") +
        '" data-setting="' + escapeHtml(field.name) + '">';
    } else {
      control = '<input type="text" autocomplete="off" data-setting="' +
        escapeHtml(field.name) + '">';
    }

    return '<div class="settings-field-row">' +
      '<span class="' + nameClass + '">' + escapeHtml(field.label || field.name) + '</span>' +
      '<span class="settings-field-control">' + control + '</span>' +
      "</div>";
  }

  // Classes, not ids, for everything below - this markup is injected by
  // renderSettingsCategory() every time the operator switches category, and
  // the two listeners it is wired to (added once, further down, on the
  // static el.settingsFields container) are event-delegated for exactly that
  // reason: an id-based document.getElementById() lookup would have nothing
  // to find until the injection has happened at least once, and re-wiring
  // fresh listeners after every re-render would leak the old ones. Same
  // pattern el.filelistsBody's folder-toggle delegation already uses.
  function settingsPasswordSectionHtml() {
    var statusText = "Admin password: " + (state.settingsAdminPasswordSet ? "set" : "not set");
    return (
      '<div class="settings-password-row">' +
        '<span class="settings-password-status">' + escapeHtml(statusText) + "</span>" +
        '<button type="button" class="btn btn-small settings-password-toggle">Change password</button>' +
      "</div>" +
      '<form class="settings-password-form" style="display:none;">' +
        '<input type="password" class="settings-new-password" placeholder="New password" autocomplete="new-password">' +
        '<input type="password" class="settings-confirm-password" placeholder="Confirm new password" autocomplete="new-password">' +
        '<button type="submit" class="btn btn-accent btn-small">Set password</button>' +
      "</form>" +
      '<p class="settings-note settings-password-note" style="display:none;"></p>'
    );
  }

  function onSettingsFieldChange(evt) {
    var input = evt.target;
    var name = input.dataset.setting;
    if (!name) { return; }

    var newValue = (input.type === "checkbox") ? (input.checked ? "true" : "false") : input.value;
    var baselineStr = settingsValueToString(state.settingsBaseline[name]);

    if (newValue === baselineStr) {
      delete state.settingsDirty[name];
    } else {
      state.settingsDirty[name] = newValue;
    }

    var row = input.closest(".settings-field-row");
    var label = row && row.querySelector(".settings-field-name");
    if (label) {
      label.classList.toggle("is-dirty", Object.prototype.hasOwnProperty.call(state.settingsDirty, name));
    }
    updateSettingsSaveBar();
  }

  function updateSettingsSaveBar() {
    var count = Object.keys(state.settingsDirty).length;
    el.settingsSaveBtn.disabled = count === 0;
    el.settingsSavebarText.classList.toggle("is-dirty", count > 0);
    el.settingsSavebarText.textContent = count === 0
      ? "All changes saved"
      : (count + (count === 1 ? " unsaved change" : " unsaved changes"));
  }

  function renderSettingsCategory() {
    var category = (state.settingsCategories || []).filter(function (c) {
      return c.id === state.settingsActiveCategory;
    })[0];

    if (!category) {
      el.settingsFields.innerHTML = '<p class="tool-status">No settings to show.</p>';
      return;
    }

    var html = '<h2 class="settings-category-title">' + escapeHtml(category.label) + "</h2>" +
      category.fields.map(settingsFieldHtml).join("");
    if (category.id === "admin-console") {
      html += settingsPasswordSectionHtml();
    }
    el.settingsFields.innerHTML = html;

    // Values are assigned as PROPERTIES here, not concatenated into value="…"
    // in the markup above. escapeHtml() is textContent -> innerHTML, which
    // encodes & < > and leaves a double quote alone - so a value containing
    // one closed the attribute early and everything after it became markup.
    // That is BUG 2 exactly, the shape four other render functions in this
    // file carry long comments about avoiding, and the same fix they use:
    // attachFilelistsCheckboxData() assigns via .dataset for the same reason.
    //
    // Self-XSS only today - every settings writer is an authenticated
    // operator and no IRC input reaches these fields - which is an argument
    // about blast radius, not about the code being right.
    var byName = {};
    category.fields.forEach(function (field) { byName[field.name] = field; });

    el.settingsFields.querySelectorAll("[data-setting]").forEach(function (input) {
      var field = byName[input.dataset.setting];
      if (field && input.type !== "checkbox") {
        var dirty = Object.prototype.hasOwnProperty.call(state.settingsDirty, field.name);
        input.value = dirty ? state.settingsDirty[field.name]
                            : settingsValueToString(field.value);
      }
      // "input" for anything typed into, so the save bar tracks a keystroke at
      // a time; "change" for the controls that have no intermediate state.
      var discrete = input.type === "checkbox" || input.tagName === "SELECT";
      input.addEventListener(discrete ? "change" : "input", onSettingsFieldChange);
    });
  }

  function renderSettingsRail() {
    var categories = state.settingsCategories || [];
    if (!categories.length) {
      el.settingsRail.innerHTML = '<div class="settings-rail-empty">No settings available.</div>';
      return;
    }
    el.settingsRail.innerHTML = categories.map(function (category) {
      var active = category.id === state.settingsActiveCategory;
      return '<button type="button" class="settings-rail-item' + (active ? " is-active" : "") +
        '" data-category="' + escapeHtml(category.id) + '">' + escapeHtml(category.label) + "</button>";
    }).join("");

    el.settingsRail.querySelectorAll("[data-category]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        state.settingsActiveCategory = btn.dataset.category;
        renderSettingsRail();
        renderSettingsCategory();
      });
    });
  }

  function showSettingsStatus(el_, message, isError) {
    el_.textContent = message;
    el_.className = "settings-note " + (isError ? "is-error" : "is-success");
    el_.style.display = "block";
  }

  // `preserveDirty` is true on the reload that follows a successful save:
  // the just-written values become the new baseline, but any OTHER field the
  // operator was mid-edit on (a save sends only the dirty set, not the whole
  // form) must not be reloaded out from under them.
  function loadSettings(preserveDirty) {
    if (!preserveDirty) {
      el.settingsFields.innerHTML = '<p class="tool-status">Loading…</p>';
    }
    fetchJson("/api/settings")
      .then(function (payload) {
        markConnection(true);
        state.settingsLoaded = true;
        state.settingsCategories = payload.categories || [];
        state.settingsAdminPasswordSet = !!payload.admin_password_set;

        var baseline = {};
        state.settingsCategories.forEach(function (category) {
          category.fields.forEach(function (field) { baseline[field.name] = field.value; });
        });
        state.settingsBaseline = baseline;
        if (!preserveDirty) { state.settingsDirty = {}; }

        var stillValid = state.settingsCategories.some(function (c) {
          return c.id === state.settingsActiveCategory;
        });
        if (!stillValid) {
          state.settingsActiveCategory = state.settingsCategories.length
            ? state.settingsCategories[0].id : null;
        }

        renderSettingsRail();
        renderSettingsCategory();
        updateSettingsSaveBar();
      })
      .catch(function (err) {
        markConnection(false);
        el.settingsFields.innerHTML =
          '<p class="tool-status is-error">Could not load settings: ' + escapeHtml(err.message) + "</p>";
      });
  }

  // Delegated on the static container, once - see settingsPasswordSectionHtml()'s
  // comment for why (the toggle/form/note only exist after the admin-console
  // category has actually been rendered at least once).
  el.settingsFields.addEventListener("click", function (evt) {
    var toggle = evt.target.closest(".settings-password-toggle");
    if (!toggle) { return; }
    var form = el.settingsFields.querySelector(".settings-password-form");
    if (form) { form.style.display = (form.style.display === "none") ? "flex" : "none"; }
  });

  el.settingsFields.addEventListener("submit", function (evt) {
    var form = evt.target.closest(".settings-password-form");
    if (!form) { return; }
    evt.preventDefault();

    var newPasswordInput = form.querySelector(".settings-new-password");
    var confirmPasswordInput = form.querySelector(".settings-confirm-password");
    var note = el.settingsFields.querySelector(".settings-password-note");
    var newPassword = newPasswordInput.value;
    var confirmPassword = confirmPasswordInput.value;

    postJson("/api/settings/password", {
      new_password: newPassword, confirm_password: confirmPassword
    }).then(function (res) {
      // Cleared unconditionally, success or not - a plaintext password must
      // never sit in the DOM a moment longer than it has to.
      newPasswordInput.value = "";
      confirmPasswordInput.value = "";
      note.style.display = "block";
      if (res.ok) {
        note.textContent = "Password changed. Rehashing…";
        note.className = "settings-note settings-password-note is-success";
        state.settingsAdminPasswordSet = true;
        form.style.display = "none";
        loadSettings(true);
      } else {
        note.textContent = (res.data && res.data.error) || "Could not change the password.";
        note.className = "settings-note settings-password-note is-error";
      }
    }).catch(function () {
      newPasswordInput.value = "";
      confirmPasswordInput.value = "";
      note.style.display = "block";
      note.textContent = "Could not reach the dashboard.";
      note.className = "settings-note settings-password-note is-error";
    });
  });

  el.settingsSaveBtn.addEventListener("click", function () {
    var dirty = state.settingsDirty;
    if (!Object.keys(dirty).length) { return; }

    el.settingsSaveBtn.disabled = true;
    el.settingsSaveStatus.style.display = "none";
    el.settingsRestartNote.style.display = "none";
    el.settingsSavebarText.textContent = "Saving…";

    postJson("/api/settings", dirty)
      .then(function (res) {
        if (res.ok) {
          state.settingsDirty = {};
          showSettingsStatus(el.settingsSaveStatus,
            "Saved " + Object.keys(dirty).length +
            (Object.keys(dirty).length === 1 ? " setting" : " settings") + ". Rehash started.", false);
          if (res.data.restart_required && res.data.restart_required.length) {
            showSettingsStatus(el.settingsRestartNote,
              "Restart required to apply: " + res.data.restart_required.join(", ") + ".", true);
          }
          loadSettings(true);
        } else {
          showSettingsStatus(el.settingsSaveStatus,
            (res.data && res.data.error) || "Could not save settings.", true);
          updateSettingsSaveBar();
        }
      })
      .catch(function () {
        showSettingsStatus(el.settingsSaveStatus, "Could not reach the dashboard.", true);
        updateSettingsSaveBar();
      });
  });

  // ------------------------------------------------------------------ Init

  // The sidebar status card is useful on every view, not only Queue, so it
  // refreshes independently of which view is active.
  loadQueue();
  setInterval(function () {
    // Keep the sidebar status fresh always; refresh the visible table only
    // when it is the one showing, so a search result is never clobbered by a
    // background poll.
    fetchJson("/api/queue").then(function (rows) {
      markConnection(true);
      renderSidebarStatus(rows);
      if (state.active === "queue") {
        renderQueueStats(rows);
        renderQueueTable(rows);
      }
    }).catch(function () { markConnection(false); });
  }, REFRESH_MS);

  // Downloads can complete while the operator is looking at a different
  // view, so this polls independently of which tab is active - same
  // reasoning as the sidebar status card above.
  setInterval(loadDownloads, DOWNLOADS_POLL_MS);

  // Only while Stats is the view on screen. Speed now and the queue counters
  // move second to second; the rest of the page does not, and polling a view
  // nobody is looking at is the 401 storm in miniature.
  setInterval(function () {
    if (state.active === "stats") { loadStats(); }
  }, REFRESH_MS);

  // A list-fetch (Download tab, or the File Lists fetch box) can complete
  // while the operator is on any other view - keep the switcher's options
  // fresh regardless of which tab is showing, same reasoning as above.
  setInterval(pollFilelistsBots, FILELISTS_BOTS_POLL_MS);
  pollFilelistsBots();

  // ---------------------------------------------------------------- Stats

  function setStat(node, value) {
    if (node) { node.textContent = value; }
  }

  function renderTopTable(node, rows, emptyText) {
    if (!node) { return; }
    if (!rows || !rows.length) {
      node.innerHTML = emptyRow(2, emptyText);
      return;
    }
    node.innerHTML = rows.map(function (row) {
      return "<tr><td>" + escapeHtml(row.name) +
             "</td><td class=\"col-num\">" + escapeHtml(String(row.count)) + "</td></tr>";
    }).join("");
  }

  function renderTopDownloads(top) {
    top = top || {};
    renderTopTable(el.stTopFiles, top.files, "Nothing sent yet.");

    // With folder packing off no album can ever be sent, so an empty table
    // would sit there for ever explaining nothing. Counts from before it was
    // switched off are still shown - they are history.
    var haveAlbums = top.albums && top.albums.length;
    var show = top.albums_enabled !== false || haveAlbums;
    if (el.stTopAlbumsWrap) { el.stTopAlbumsWrap.style.display = show ? "" : "none"; }
    if (el.stTopAlbumsLabel) { el.stTopAlbumsLabel.style.display = show ? "" : "none"; }
    if (el.stTopAlbumsOff) { el.stTopAlbumsOff.style.display = show ? "none" : ""; }
    if (show) {
      renderTopTable(el.stTopAlbums, top.albums, "No album sent yet.");
    }
  }

  function renderStats(data) {
    var t = data.transfer || {};
    var s = data.sent || {};
    var lib = data.library || {};

    // Every figure is rendered server-side by the same helpers the channel
    // advert and the admin console use, so the page cannot disagree with the
    // advert about how the same number reads. The raw values are in the
    // payload too, for anything that is not this page.
    setStat(el.stSpeed, t.speed_now_text || "0k/s");
    setStat(el.stRecord, t.record_text || "0k/s");
    setStat(el.stSending, (t.sending || 0) + " / " + (t.slots || 0));
    setStat(el.stQueued, (t.queued_files || 0).toLocaleString());
    setStat(el.stQueuedLabel,
            "Queued" + (t.queued_users ? " · " + t.queued_users + " user" +
                        (t.queued_users === 1 ? "" : "s") : ""));
    setStat(el.stUptime, t.uptime_text || "0 Min");

    setStat(el.stSentTotal, s.total_text || "0B");
    setStat(el.stSentToday, s.today_text || "0B");
    setStat(el.stSentYesterday, s.yesterday_text || "0B");
    setStat(el.stSentTotalFiles, "Total · " + (s.total_files || 0).toLocaleString() + " files");
    setStat(el.stSentTodayFiles, "Today · " + (s.today_files || 0).toLocaleString() + " files");
    setStat(el.stSentYesterdayFiles,
            "Yesterday · " + (s.yesterday_files || 0).toLocaleString() + " files");

    setStat(el.stFiles, (lib.files || 0).toLocaleString());
    setStat(el.stSize, lib.size || "0B");
    // null means "no RAR list has been built", which is not the same claim as
    // "this bot offers no albums" - so it shows as unknown rather than zero.
    setStat(el.stAlbums, lib.rar_folders === null || lib.rar_folders === undefined
            ? "—" : lib.rar_folders.toLocaleString());
    setStat(el.stBuilt, lib.list_date || "—");

    renderTopDownloads(data.top);
    setStat(el.stFoot, data.version || "");
  }

  function loadStats() {
    fetchJson("/api/stats").then(function (data) {
      markConnection(true);
      renderStats(data);
    }).catch(function () { markConnection(false); });
  }

  // ---------------------------------------------------------------- Theme
  //
  // The stored choice is already on the root element by the time this runs -
  // index.html sets it in the head, before the first paint, so the page never
  // renders in one theme and swaps to the other. All this does is keep the two
  // buttons in step with it and write the choice down.
  //
  // Per browser, not per bot: which theme suits a screen is a fact about the
  // person looking at it, and the same daemon gets looked at from a phone in a
  // dark room and a desktop by a window. Nothing about it goes to the server.

  var THEME_KEY = "dccore-theme";

  function storedTheme() {
    // Browsers set to block site data throw on access rather than returning
    // null, so this cannot be an unguarded read.
    try {
      var saved = localStorage.getItem(THEME_KEY);
      return (saved === "dark" || saved === "light") ? saved : null;
    } catch (err) {
      return null;
    }
  }

  function currentTheme() {
    // No stored choice means no attribute, and the operating system decides -
    // so ask it the same question the stylesheet's media query asks, rather
    // than assuming dark and lighting up the wrong button.
    var chosen = document.documentElement.getAttribute("data-theme");
    if (chosen === "dark" || chosen === "light") {
      return chosen;
    }
    return (window.matchMedia &&
            window.matchMedia("(prefers-color-scheme: light)").matches)
      ? "light" : "dark";
  }

  function markThemeButtons() {
    var active = currentTheme();
    [el.themeDark, el.themeLight].forEach(function (button) {
      if (!button) {
        return;
      }
      var mine = button.getAttribute("data-theme-choice") === active;
      button.classList.toggle("is-active", mine);
      button.setAttribute("aria-pressed", mine ? "true" : "false");
    });
  }

  function chooseTheme(name) {
    document.documentElement.setAttribute("data-theme", name);
    try {
      localStorage.setItem(THEME_KEY, name);
    } catch (err) {
      // A private window cannot remember it. The page still changes now, which
      // is what was asked for; it simply starts over on the next load.
    }
    markThemeButtons();
  }

  [el.themeDark, el.themeLight].forEach(function (button) {
    if (button) {
      button.addEventListener("click", function () {
        chooseTheme(button.getAttribute("data-theme-choice"));
      });
    }
  });

  // Somebody who has never picked follows their machine, so follow it when it
  // changes too - a laptop switching to dark at sunset should take the
  // dashboard with it. An explicit choice is left alone.
  if (window.matchMedia) {
    var watcher = window.matchMedia("(prefers-color-scheme: light)");
    var onSystemChange = function () {
      if (!storedTheme()) {
        markThemeButtons();
      }
    };
    if (watcher.addEventListener) {
      watcher.addEventListener("change", onSystemChange);
    } else if (watcher.addListener) {
      watcher.addListener(onSystemChange);      // Safari before 14
    }
  }

  markThemeButtons();

  activateView("search");
})();
