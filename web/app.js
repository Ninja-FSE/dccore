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
  var CONSOLE_LOG_POLL_MS = 2000;
  // Three elements per log line, so this is comfortably more than the
  // server's own 500-line buffer holds - the cap exists to stop unbounded
  // growth over hours, not to second-guess how much history is useful.
  var CONSOLE_LOG_MAX_ELEMENTS = 3000;
  // #439: while disabled, retried at this much slower cadence instead of
  // stopping outright - so ticking WEBUI_CONSOLE_ENABLED on in Settings and
  // saving (no restart needed; the save's own rehash applies it live) is
  // noticed here within seconds, instead of the log staying dead until an
  // F5. Slow enough that a dashboard with the Console off - the shipped
  // default on any non-loopback install - is not hammering a 404 route
  // every couple of seconds forever.
  var CONSOLE_LOG_RECHECK_MS = 15000;
  // Held so the poll can swap its own cadence - see disableConsoleUi() and
  // enableConsoleUiIfNeeded().
  var consoleLogTimer = null;
  // Matches webserver.py's FILELISTS_DEFAULT_PAGE_SIZE - keep the two in
  // sync if either changes, so a page here always lines up with a page the
  // server actually hands back.
  var FILELISTS_PAGE_SIZE = 200;
  // Short, because the query behind it is measured in single-digit
  // milliseconds - the index exists so this can be a filter rather than a
  // search button. It is not zero: a keystroke still costs a round trip, and
  // a fast typist should not queue one per character.
  var FILELISTS_FILTER_DEBOUNCE_MS = 120;

  var views = {
    search:    { title: "Search",     sub: "Find a file across the current master list." },
    download:  { title: "Downloads",  sub: "What you have asked other bots for, and how it is going." },
    filelists: { title: "List Browser", sub: "Every file this bot - or a fetched bot's list - is currently offering." },
    tools:     { title: "Tools",      sub: "Checks you run on demand against the current master list." },
    // Reached from the badge in the status panel, not from the nav rail:
    // it is somewhere you are SENT when something happened, not somewhere
    // you go looking. A permanent nav entry for a page that is empty almost
    // always is a permanent reminder of nothing.
    notices:   { title: "What happened", sub: "Kicks, bans and rebuilds that need looking at." },
    messages:  { title: "Messages",   sub: "People who spoke to the bot privately and got no answer." },
    settings:  { title: "Settings",   sub: "Every editable setting, grouped. Saving writes settings.conf and starts a rehash." },
    stats:     { title: "Stats",      sub: "Everything this bot knows about itself, including who is waiting." },
    console:   { title: "Console",    sub: "The DCC CHAT admin console's commands and live log, in the browser." }
  };

  var state = {
    // What the previewed OmenServe import would write, held between the
    // preview and the confirm so the button sends exactly what was shown -
    // not a second parse that could have moved on from it.
    importValues: null,
    // Served folders (#164 step 4). `foldersDraft` is what the rows are
    // showing and `folders` is what the server last confirmed - kept apart
    // so switching category and back does not silently discard an edit,
    // and so a failed save leaves the operator's rows exactly as typed.
    // The last checkbox the operator touched, as the anchor for a
    // shift-click range. A DOM node, so it is cleared whenever the
    // table is rebuilt.
    filelistsLastChecked: null,
    // One row per held list, keyed by bot, as /api/filelists/bots last
    // reported it - so the staleness banner can be rendered for whichever
    // source is selected without asking again.
    filelistsBots: {},
    folders: null, foldersSource: "", foldersDraft: null, foldersNote: null,
    downloads: [],
    lists: null, listsSource: "", listsDraft: null, listsNote: null,
    onConnect: null, onConnectNote: null,
    // The folder picker (#164 step 5). `browse` is null when the panel is
    // closed; open, it carries the row it will write back into.
    foldersBrowserEnabled: false, browse: null,
    active: "search", filelistsLoaded: false, filelistsSource: "__own__",
    // The live filter term, and the token that decides whether a reply is
    // still wanted. Typing fast enough puts several requests in flight, and
    // the slowest is not necessarily the oldest.
    filelistsFilter: "", filelistsFilterToken: 0,
    // Which LOAD is allowed to render. Separate from filelistsFilterToken,
    // which decides which debounced keystroke gets to send a request at all -
    // two different questions, and conflating them is what let a stale reply
    // through.
    filelistsLoadToken: 0,
    // The per-list search (#399's follow-up) - a different question from the
    // sidebar's filelistsFilter above: this one narrows the SINGLE open
    // list's own rows server-side, rather than spanning every list held and
    // replacing the whole view. Own debounce token, same reasoning as
    // filelistsFilterToken's comment; the reply itself is still guarded by
    // filelistsLoadToken, which every load shares regardless of what
    // triggered it.
    filelistsListQuery: "", filelistsListQueryToken: 0,
    // Which bots the operator has switched OFF while filtering, and the last
    // answer the server gave. Toggling re-renders from that answer rather
    // than asking again: the rows are already here, and a round trip per
    // click would be slower than the search that produced them.
    filelistsExcluded: {}, filelistsFilterPayload: null, filelistsMatchTerms: [],
    // Whether what is on screen has any folders in it - see listIsFlat().
    filelistsFlat: false,
    // Off for every new term. A row put back on screen while looking for one
    // thing should not still be there, unasked, while looking for the next.
    filelistsRevealEmpty: false,
    filelistsOffset: 0, filelistsTotal: 0, filelistsReturned: 0,
    // Whether the server's row ceiling - not the end of the list - is why
    // this page has fewer folders on it than were asked for (#477).
    filelistsRowCapped: false,
    filelistsHistory: [],
    settingsLoaded: false, settingsCategories: [], settingsActiveCategory: null,
    settingsBaseline: {}, settingsDirty: {}, settingsAdminPasswordSet: false,
    consoleCursor: 0
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
    filelistsFilterInput: document.getElementById("filelists-filter-input"),
    filelistsFilterClear: document.getElementById("filelists-filter-clear"),
    filelistsFilterStatus: document.getElementById("filelists-filter-status"),
    filelistsFilterActions: document.getElementById("filelists-filter-actions"),
    filelistsFilterAll: document.getElementById("filelists-filter-all"),
    filelistsFilterNone: document.getElementById("filelists-filter-none"),
    filelistsFilterReveal: document.getElementById("filelists-filter-reveal"),
    statusNotices:  document.getElementById("status-notices"),
    noticeBadge:    document.getElementById("notice-badge"),
    noticeList:     document.getElementById("notice-list"),
    messageList:     document.getElementById("message-list"),
    messagesMarkRead: document.getElementById("messages-mark-read"),
    messagesNavCount: document.getElementById("messages-nav-count"),
    noticesMarkRead: document.getElementById("notices-mark-read"),
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
    filelistsFreshness: document.getElementById("filelists-freshness"),
    filelistsListTabs: document.getElementById("filelists-list-tabs"),
    filelistsListSearchInput: document.getElementById("filelists-list-search-input"),
    // filelistsPurgeListBtn, not filelistsPurgeBtn: #388 is adding a BULK
    // "purge every offline bot's list" button to the toolbar under that
    // exact name. Two keys with the same name in this object literal merge
    // cleanly in git and then silently keep the last one, so one of the two
    // buttons would stop working with no error anywhere.
    filelistsPurgeListBtn: document.getElementById("filelists-purge-btn"),
    filelistsBotList: document.getElementById("filelists-bot-list"),
    filelistsPurgeBtn:    document.getElementById("filelists-purge-offline-btn"),
    filelistsPurgeStatus: document.getElementById("filelists-purge-status"),
    filelistsPrevBtn:     document.getElementById("filelists-prev-btn"),
    filelistsNextBtn:     document.getElementById("filelists-next-btn"),
    filelistsPageInfo:    document.getElementById("filelists-page-info"),
    filelistsExpandAll:   document.getElementById("filelists-expand-all"),
    filelistsCollapseAll: document.getElementById("filelists-collapse-all"),
    filelistsHeadCheck:   document.getElementById("filelists-head-check"),
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
    importFile:            document.getElementById("import-file"),
    importPasteToggle:     document.getElementById("import-paste-toggle"),
    importPaste:           document.getElementById("import-paste"),
    importPasteActions:    document.getElementById("import-paste-actions"),
    importPasteRead:       document.getElementById("import-paste-read"),
    importStatus:          document.getElementById("import-status"),
    importPreviewWrap:     document.getElementById("import-preview-wrap"),
    importPreview:         document.getElementById("import-preview"),
    importWarning:         document.getElementById("import-warning"),
    importConfirm:         document.getElementById("import-confirm"),
    importApply:           document.getElementById("import-apply"),
    importCancel:          document.getElementById("import-cancel"),
    stTopAlbums:           document.getElementById("st-top-albums"),
    stTopAlbumsWrap:       document.getElementById("st-top-albums-wrap"),
    stTopAlbumsLabel:      document.getElementById("st-top-albums-label"),
    stTopAlbumsOff:        document.getElementById("st-top-albums-off"),
    themeDark:    document.getElementById("theme-dark"),
    themeLight:   document.getElementById("theme-light"),
    updateListRunBtn:     document.getElementById("update-list-run-btn"),
    updateListStatus:     document.getElementById("update-list-status"),
    updateListBar:        document.getElementById("update-list-bar"),
    updateListBarFill:    document.getElementById("update-list-bar-fill"),
    verifyRunBtn:         document.getElementById("verify-run-btn"),
    verifyStatus:         document.getElementById("verify-status"),
    verifyResults:        document.getElementById("verify-results"),
    settingsRail:         document.getElementById("settings-rail"),
    settingsFields:       document.getElementById("settings-fields"),
    settingsSaveBtn:      document.getElementById("settings-save-btn"),
    settingsSavebarText:  document.getElementById("settings-savebar-text"),
    settingsRestartNote:  document.getElementById("settings-restart-note"),
    settingsSaveStatus:   document.getElementById("settings-save-error"),
    consoleLog:     document.getElementById("console-log"),
    consoleForm:    document.getElementById("console-form"),
    consoleInput:   document.getElementById("console-input"),
    consoleRunBtn:  document.getElementById("console-run-btn")
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
  // Like fetchJson, but a non-2xx is DATA rather than a throw - the same
  // posture postJson already takes below, and for the same reason: these
  // routes answer a bad request with a JSON body that says what was wrong,
  // and fetchJson's `throw new Error("HTTP " + res.status)` discards it. The
  // folder browser's "... is not a folder on this machine" reached the
  // operator as "HTTP 400" until this existed.
  function fetchJsonAllowingError(url) {
    return fetch(url, { headers: { Accept: "application/json" } })
      .then(function (res) {
        if (res.status === 401) {
          window.location.href = "/login";
          return new Promise(function () {});
        }
        return res.json().then(function (data) {
          return { ok: res.ok, status: res.status, data: data };
        });
      });
  }

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
    // Guarded, because one missing section must not take the whole navigation
    // with it. Unguarded, a null here threw before the per-view loaders at the
    // bottom of this function ran, so every OTHER view silently stopped
    // working too - one absent element disabling the entire dashboard.
    Object.keys(views).forEach(function (key) {
      var section = document.getElementById("view-" + key);
      if (section) { section.classList.toggle("is-active", key === name); }
    });
    el.navItems.forEach(function (btn) {
      btn.classList.toggle("is-active", btn.dataset.view === name);
    });
    el.pageTitle.textContent = views[name].title;
    el.pageSub.textContent = views[name].sub;

    if (name === "download") { loadDownloads(); }
    if (name === "filelists") {
      pollFilelistsBots();
      if (!state.filelistsLoaded) { loadFilelists(); }
    }
    if (name === "settings" && !state.settingsLoaded) { loadSettings(); }
    if (name === "stats") { loadStats(); }
    // Loaded here rather than in the badge's own handler, so every way into
    // this view draws it - the badge is the usual one, not the only one.
    if (name === "notices") { loadNotices(true); }
    if (name === "messages") { loadMessages(true); }
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
      // CrateBot finds 12 and sends 5, and saying only "5" would hide that
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

  // The pill an operator reads, which is NOT the internal state name.
  //
  // From a maintainer, on seeing two list rows sitting at "OFFERED": "should
  // be REQUESTED, not offered". Exactly right, and the word was backwards on
  // the screen rather than in the queue. dcc_fetch.py flips a row to
  // `offered` in check_fetch_queue() at the moment it DISPATCHES OUR OWN
  // request line - `@bot` for a list, `!bot <file>` otherwise - and stamps
  // `offered_at` with the time we sent it. So the state means "we have asked
  // and are waiting for their DCC SEND". Nothing has been offered to us; the
  // name reads from inside the module, where the row IS the offer we are
  // waiting on.
  //
  // The internal name stays as it is. It is written into the fetch queue
  // file, so renaming it would strand every row in flight across a restart,
  // and it is matched by name in a dozen places in dcc_fetch.py. Only the
  // word on the pill is wrong, so only the word on the pill changes. The CSS
  // class is still built from the state name, so .status-offered keeps
  // styling it.
  //
  // "rejected" is not a state dcc_fetch.py ever writes. A list archive whose
  // bytes arrived intact but which the extraction guard refused keeps
  // state === "complete", because the transfer really did succeed - the
  // reason it was refused is carried separately, in list_processing_error.
  // This is the display-side name for that combination.
  var DOWNLOAD_STATE_LABELS = {
    pending: "Pending", offered: "Requested", listening: "Listening",
    receiving: "Receiving", complete: "Complete", failed: "Failed",
    rejected: "Rejected"
  };

  function loadDownloads() {
    fetchJson("/api/fetch/status").then(function (rows) {
      markConnection(true);
      // Held so a row's own details can be looked up by id. The Redownload
      // button needs the bot and the filename, and neither may go into a data
      // attribute - escapeHtml() is textContent -> innerHTML and leaves a
      // double quote alone, and both of those come off the wire. The id does
      // go in one: it is ours, and it is hex.
      state.downloads = rows || [];
      renderDownloads(rows);
    }).catch(function () { markConnection(false); });
  }

  function redownloadFetchRow(button) {
    var requestId = decodeURIComponent(button.dataset.requestId);
    var row = (state.downloads || []).filter(function (candidate) {
      return String(candidate.id) === requestId;
    })[0];
    if (!row) { return; }

    button.disabled = true;

    // A LIST row and a FILE row are asked for in different ways, because they
    // always were: a list is "@<bot>" and we cannot know what the bot will
    // call its archive, while a file is named outright. Re-asking has to use
    // the same route the original request came through, or the retry would
    // create a row of a different kind from the one it is retrying.
    var again;
    if (row.request_type === "list") {
      again = postJson("/api/filelists/fetch", { bot: row.bot });
    } else {
      // requested_filename, not filename: for a folder row the second is the
      // name the OTHER bot eventually advertised, and for a failed one it may
      // never have been set at all. The first is what we asked for.
      var wanted = row.requested_filename || row.filename;
      if (!wanted) { button.disabled = false; return; }
      again = postJson("/api/fetch/enqueue", [{ bot: row.bot, filename: wanted }]);
    }

    again.then(function (res) {
      if (!res.ok) {
        window.alert("Could not ask again: " +
          ((res.data && res.data.error) || ("HTTP " + res.status)));
        button.disabled = false;
        return;
      }
      // The old row is left alone deliberately. It is the record of what
      // happened, and deleting it as a side effect of retrying would throw
      // away the reason the retry was needed.
      loadDownloads();
    }).catch(function (err) {
      window.alert("Could not ask again: " + err.message);
      button.disabled = false;
    });
  }

  // Delegated: renderDownloads() rebuilds the table's innerHTML on every
  // poll, which would silently drop a listener attached to any one row.
  el.downloadsBody.addEventListener("click", function (evt) {
    var retry = evt.target.closest ? evt.target.closest(".fetch-retry-btn") : null;
    if (retry) {
      redownloadFetchRow(retry);
      return;
    }

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
      // ASK AGAIN, for a row that did not arrive. Requested: a failed or rejected
      // fetch is the one an operator most wants to retry, and the only way to
      // do it was to go back to the List Browser and retype the nick.
      //
      // Not offered on a row that succeeded: there is a Download button there,
      // and re-fetching a list already held is what the Refresh in the List
      // Browser is for.
      //
      // NOTHING IN AN ATTRIBUTE that a nick or a filename could break out of.
      // escapeHtml() is textContent -> innerHTML and leaves a double quote
      // alone, and both of these come off the wire - so the row's id goes in
      // (it is ours, and hex) and the handler looks the rest up from state.
      var retryBtn = (rejected || state === "failed")
        ? "<button type=\"button\" class=\"btn btn-small fetch-retry-btn\" data-request-id=\"" +
          encodeURIComponent(row.id) + "\">Redownload</button> "
        : "";
      var action;
      if (rejected) {
        action = "<span class=\"col-dim\">" + escapeHtml(row.list_processing_error) + "</span> " + retryBtn + deleteBtn;
      } else if (state === "complete" && row.stored_filename) {
        action = "<a class=\"btn btn-small\" href=\"/api/fetch/" + encodeURIComponent(row.id) + "/download\">Download</a> " + deleteBtn;
      } else if (state === "complete") {
        // A fetched list, extracted - dcc_fetch.py already removed its zip.
        action = "<span class=\"col-dim\">Browse it in List Browser</span> " + deleteBtn;
      } else if (state === "failed") {
        action = "<span class=\"col-dim\">" + escapeHtml(row.reason || "") + "</span> " + retryBtn + deleteBtn;
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

  // The bar under a sending row. null/undefined "size" means dcc.py has not
  // recorded the file's real size yet (a brand-new dispatch, for the brief
  // window before start_dcc_send() reads it off disk) - not "0% so far",
  // which a size of 0 would also produce honestly for a genuinely empty
  // file. No bar at all is the correct rendering of "not known yet".
  function queueProgressBar(row) {
    if (!row.size) { return ""; }
    var pct = Math.max(0, Math.min(100, Math.round(100 * (row.bytes_sent || 0) / row.size)));
    return "<div class=\"progress-bar queue-progress\">" +
      "<div class=\"progress-bar-fill\" style=\"width:" + pct + "%\"></div></div>";
  }

  // What is WAITING - never includes whatever is currently sending, because
  // dcc.py never puts the in-flight file into dcc_queue (see
  // build_queue_payload()'s own docstring). A single queued file is shown
  // plain, like before; more than one collapses behind a <details> so the
  // row does not grow without bound, but every one of them is there to open
  // - not just a preview of the first, which is all this used to show.
  function queueFileList(row) {
    var files = row.files || [];
    if (!files.length) { return ""; }
    if (files.length === 1) { return escapeHtml(files[0]); }
    return "<details class=\"queue-files\"><summary>" +
      escapeHtml(files[0]) + " <span class=\"col-dim\">(+" + (files.length - 1) + " more)</span></summary>" +
      "<ul class=\"queue-file-list\">" +
      files.map(function (f) { return "<li>" + escapeHtml(f) + "</li>"; }).join("") +
      "</ul></details>";
  }

  function renderQueueTable(rows) {
    if (!rows.length) {
      el.queueBody.innerHTML = emptyRow(4, "The queue is empty.");
      return;
    }
    el.queueBody.innerHTML = rows.map(function (row) {
      var status = row.status || "queued";
      var label = STATUS_LABELS[status] || status;

      var sendingPart = "";
      if (status === "sending" && row.current_file) {
        sendingPart = "<div class=\"queue-current\">Sending: " +
          escapeHtml(row.current_file) + "</div>" + queueProgressBar(row);
      }
      // With nothing queued behind an in-flight send, "preview" already
      // equals current_file - showing it a second time would be a
      // duplicate, not new information.
      var queuedPart = (row.files || []).length ? queueFileList(row)
        : (sendingPart ? "" : escapeHtml(row.preview));

      return "<tr>" +
        "<td class=\"col-mono\">" + escapeHtml(row.user) + "</td>" +
        "<td class=\"col-dim col-mono\">" + sendingPart + queuedPart + "</td>" +
        "<td class=\"col-mono\">" + escapeHtml(row.count) + "</td>" +
        "<td><span class=\"status-pill status-" + escapeHtml(status) + "\">" + escapeHtml(label) + "</span></td>" +
        "</tr>";
    }).join("");
  }

  // WHAT THE OPERATOR MISSED. Two severities and no more: "warning" happened
  // and is over, "error" is still true. A third would be a third thing nobody
  // can tell apart at a glance, and a glance is the only moment a badge is
  // read.
  //
  // The count and the colour come from the SERVER, which knows the read
  // marker. Working them out here would mean the page holding a copy of that
  // marker, and two places deciding whether to light up is two places that
  // can disagree.
  function renderNoticeBadge(payload) {
    if (!el.statusNotices || !el.noticeBadge) { return; }
    var unread = (payload && payload.unread) || 0;

    // Hidden entirely when there is nothing, rather than showing a zero. A
    // badge that is always there is furniture; one that appears is a message.
    el.statusNotices.hidden = unread === 0;
    if (!unread) { return; }

    el.noticeBadge.textContent =
      unread + (unread === 1 ? " thing to see" : " things to see");
    el.noticeBadge.className = "notice-badge is-" +
      (payload.severity === "error" ? "error" : "warning");
  }

  function loadNotices(render) {
    return fetchJson("/api/notices").then(function (payload) {
      renderNoticeBadge(payload);
      if (render) { renderNoticeList(payload); }
      return payload;
    }).catch(function () {
      // A dashboard that cannot reach the daemon has bigger problems and
      // already says so elsewhere. Never leave a stale count on screen.
      if (el.statusNotices) { el.statusNotices.hidden = true; }
    });
  }

  function renderNoticeList(payload) {
    if (!el.noticeList) { return; }
    var rows = (payload && payload.notices) || [];
    if (!rows.length) {
      el.noticeList.innerHTML =
        '<p class="notice-empty">Nothing has needed your attention.</p>';
      return;
    }
    var seen = (payload && payload.seen_id) || 0;
    el.noticeList.innerHTML = rows.map(function (row) {
      var fresh = (row.id || 0) > seen;
      return '<div class="notice-row is-' +
        (row.severity === "error" ? "error" : "warning") +
        (fresh ? " is-unread" : "") + '">' +
        '<span class="notice-when">' + escapeHtml(noticeWhen(row.at)) + "</span>" +
        '<span class="notice-text">' + escapeHtml(row.text || "") + "</span>" +
        "</div>";
    }).join("");
  }

  // The date as well as the time once it is not today: "14:32" on a notice
  // from this morning is clear, and on one from last week it is a lie by
  // omission - and a notice from last week is exactly the kind this exists
  // to keep.
  function noticeWhen(at) {
    if (!at) { return ""; }
    var when = new Date(at * 1000);
    var now = new Date();
    var sameDay = when.getFullYear() === now.getFullYear()
      && when.getMonth() === now.getMonth()
      && when.getDate() === now.getDate();
    var clock = String(when.getHours()).padStart(2, "0") + ":" +
                String(when.getMinutes()).padStart(2, "0");
    if (sameDay) { return clock; }
    return String(when.getDate()).padStart(2, "0") + "/" +
           String(when.getMonth() + 1).padStart(2, "0") + " " + clock;
  }

  // AN INBOX FOR A BOT THAT NEVER REPLIES. Everything here looks like a
  // conversation and is not one, which is why the count lives on the nav item
  // rather than on the status badge beside the notices: a notice is something
  // that went wrong and wants you now, a message is something waiting for you
  // whenever you next look. Mixing them would make one of the two mean less.
  function renderMessagesCount(payload) {
    if (!el.messagesNavCount) { return; }
    var unread = (payload && payload.unread) || 0;
    el.messagesNavCount.hidden = unread === 0;
    el.messagesNavCount.textContent = unread > 99 ? "99+" : String(unread);
  }

  // The Console's off-switch, followed exactly. HIDDEN, not removed, and
  // #view-messages stays in the DOM: activateView() walks every key in
  // `views` and touches getElementById("view-" + key).classList for each, so
  // deleting the section makes that throw on EVERY view switch and takes the
  // rest of the navigation down with it. That was found on a real install
  // once already - see disableConsoleUi() for the full account.
  function disableMessagesUi() {
    var navButton = document.querySelector(".nav-item[data-view=\"messages\"]");
    if (navButton) { navButton.hidden = true; }
    if (el.messagesNavCount) { el.messagesNavCount.hidden = true; }
    if (state.active === "messages") { activateView("search"); }
  }

  function loadMessages(render) {
    return fetchJson("/api/messages").then(function (payload) {
      renderMessagesCount(payload);
      if (render) { renderMessageList(payload); }
      return payload;
    }).catch(function (err) {
      // 404 is the bot saying it does not keep private messages at all,
      // which is a different answer from "the request failed" - a network
      // blip must not quietly delete a page that is still there.
      if (err && String(err.message) === "HTTP 404") {
        disableMessagesUi();
        return;
      }
      if (el.messagesNavCount) { el.messagesNavCount.hidden = true; }
    });
  }

  function renderMessageList(payload) {
    if (!el.messageList) { return; }
    var rows = (payload && payload.messages) || [];
    if (!rows.length) {
      // Says what the emptiness MEANS. "No messages" reads as though
      // something might be broken; nobody having needed to ask is the
      // ordinary, good state.
      el.messageList.innerHTML =
        '<p class="message-empty">Nobody has messaged the bot. Requests that ' +
        'use the right command are answered normally and do not appear here.</p>';
      return;
    }
    var seen = (payload && payload.seen_id) || 0;
    el.messageList.innerHTML = rows.map(function (row) {
      var fresh = (row.id || 0) > seen;
      return '<div class="message-row' + (fresh ? " is-unread" : "") + '">' +
        '<div class="message-head">' +
          '<span class="message-who">' + escapeHtml(row.nick || "?") + "</span>" +
          '<span class="message-when">' + escapeHtml(noticeWhen(row.at)) + "</span>" +
        "</div>" +
        '<p class="message-text">' + escapeHtml(row.text || "") + "</p>" +
        "</div>";
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

  // Purging offline bots' held lists (#385) ---------------------------------

  el.filelistsPurgeBtn.addEventListener("click", function () {
    if (!window.confirm(
        "Forget every held list whose bot is not in a channel with you " +
        "right now? A bot that is here, or one still joining, is left " +
        "alone - this only removes the red-dot rows.")) {
      return;
    }
    el.filelistsPurgeBtn.disabled = true;
    postJson("/api/filelists/purge-offline", {}).then(function (res) {
      if (!res.ok) {
        showFilelistsPurgeStatus(
          "Could not purge: " + (res.data && res.data.error || ("HTTP " + res.status)), true);
        return;
      }
      var count = res.data.count || 0;
      var skipped = (res.data.skipped_in_flight || []).length;
      var text = count === 0
        ? "Nothing to purge - no held list is currently showing the red dot."
        : "Forgot " + count + " list" + (count === 1 ? "" : "s") + ": " +
          res.data.purged.join(", ") + ".";
      if (skipped > 0) {
        text += " Left " + skipped + " alone - a fetch is still in flight for " +
          (skipped === 1 ? "it" : "them") + ".";
      }
      showFilelistsPurgeStatus(text, false);
      pollFilelistsBots();
    }).catch(function (err) {
      showFilelistsPurgeStatus("Request failed: " + err.message, true);
    }).then(function () {
      el.filelistsPurgeBtn.disabled = false;
    });
  });

  function showFilelistsPurgeStatus(text, isError) {
    el.filelistsPurgeStatus.textContent = text;
    el.filelistsPurgeStatus.classList.toggle("is-error", !!isError);
    el.filelistsPurgeStatus.hidden = false;
  }

  // Switching which bot's list is shown ------------------------------------

  // Delegated, because the rows are rebuilt on every poll - a listener per
  // row would be re-attached each time, and the row the operator is aiming
  // at can be replaced between the mousedown and the click.
  el.filelistsBotList.addEventListener("click", function (evt) {
    var row = evt.target.closest ? evt.target.closest(".bot-row") : null;
    if (!row) { return; }

    // A bot we have only seen advertising has no list to page through. Rather
    // than switching to a source that would come back empty, put its nick
    // where fetching one starts.
    if (row.dataset.held === "no") {
      el.filelistsFetchInput.value = row.dataset.nick || row.dataset.bot;
      el.filelistsFetchInput.focus();
      showFilelistsFetchStatus("No list held for " + row.dataset.bot
        + " yet. Press Fetch to ask for it.");
      return;
    }

    // WHILE FILTERING the sidebar answers a different question, so the click
    // does a different thing: the table is showing every list at once, so
    // "switch to this one" has nothing to mean. Clicking toggles whether
    // that bot's matches are on screen instead.
    //
    // KEYED BY NICK, not by the row's own dataset.bot (#399): switching a
    // bot's results on and off is a per-BOT choice - a row now stands for
    // every list that bot has, and excluding only the one list currently
    // showing would leave its other lists' matches on screen with no row
    // left to toggle them back off.
    if ((state.filelistsFilter || "").trim() && !isOwnSource(row.dataset.bot)) {
      var key = String(row.dataset.nick || row.dataset.bot || "").toLowerCase();
      if (state.filelistsExcluded[key]) {
        delete state.filelistsExcluded[key];
      } else {
        state.filelistsExcluded[key] = true;
      }
      rerenderFromFilterPayload();
      return;
    }

    // ALREADY OPEN, by bot rather than by exact list (#399): re-clicking a
    // row whose RAR tab is open must leave the RAR tab open, not silently
    // reset to the main list every time the sidebar happens to redraw.
    if (nickOfSource(row.dataset.bot) === nickOfSource(state.filelistsSource)) {
      return;
    }
    // A genuinely NEW bot always opens on its primary list; picking a
    // different one from here is what the tabs above the table are for.
    state.filelistsSource = row.dataset.bot;
    markFilelistsActiveBot();
    state.filelistsOffset = 0;
    state.filelistsHistory = [];
    // A search scoped to the PREVIOUS bot's list means nothing here - left
    // in place it would silently narrow a bot the operator never asked to
    // filter, on the very first page they see of it.
    resetFilelistsListQuery();
    loadFilelists();
  });

  // Delegated for the same reason the sidebar's listener is: the tab bar is
  // rebuilt every time the open bot or its lists change.
  el.filelistsListTabs.addEventListener("click", function (evt) {
    var tab = evt.target.closest ? evt.target.closest(".filelists-list-tab") : null;
    if (!tab || tab.dataset.bot === state.filelistsSource) { return; }
    state.filelistsSource = tab.dataset.bot;
    markFilelistsActiveBot();
    state.filelistsOffset = 0;
    state.filelistsHistory = [];
    // Switching tabs is switching which FILE this bot's search words would
    // run against - a term meant for the main list rarely means anything in
    // its RAR list, so start that list's own search fresh too.
    resetFilelistsListQuery();
    loadFilelists();
  });

  el.filelistsFilterInput.addEventListener("input", function () {
    state.filelistsFilter = el.filelistsFilterInput.value;
    runFilelistsFilter();
  });

  if (el.filelistsListSearchInput) {
    el.filelistsListSearchInput.addEventListener("input", function () {
      state.filelistsListQuery = el.filelistsListSearchInput.value;
      runFilelistsListQuery();
    });
  }

  el.filelistsFilterAll.addEventListener("click", function () {
    setEveryListShown(true);
  });

  el.filelistsFilterNone.addEventListener("click", function () {
    setEveryListShown(false);
  });

  // A toggle, not a one-way door: somebody who looked at what was hidden
  // wants to put it back without retyping the term.
  if (el.noticeBadge) {
    el.noticeBadge.addEventListener("click", function () {
      activateView("notices");
    });
  }

  if (el.messagesMarkRead) {
    el.messagesMarkRead.addEventListener("click", function () {
      postJson("/api/messages/read", {}).then(function (res) {
        if (!res.ok) {
          // Turned off while the page was open: the same 404, handled the
          // same way, rather than a button that silently does nothing.
          if (res.status === 404) { disableMessagesUi(); }
          return;
        }
        renderMessagesCount(res.data);
        renderMessageList(res.data);
      });
    });
  }

  if (el.noticesMarkRead) {
    el.noticesMarkRead.addEventListener("click", function () {
      // The answer that clears the badge is the same answer that redraws the
      // list, so the two cannot disagree about what was acknowledged.
      postJson("/api/notices/read", {}).then(function (res) {
        if (!res.ok) { return; }
        renderNoticeBadge(res.data);
        renderNoticeList(res.data);
      });
    });
  }

  el.filelistsFilterReveal.addEventListener("click", function () {
    state.filelistsRevealEmpty = !state.filelistsRevealEmpty;
    rerenderFromFilterPayload();
  });

  el.filelistsFilterClear.addEventListener("click", function () {
    el.filelistsFilterInput.value = "";
    state.filelistsFilter = "";
    runFilelistsFilter();
    el.filelistsFilterInput.focus();
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
      renderFilelistsFreshness();
    }).catch(function () { markConnection(false); });
  }

  // BUILT WITH DOM APIs, not concatenated markup. A bot nick is remote input
  // - it is whatever that bot called itself in a channel - and escapeHtml()
  // does not encode a quote, so a nick in an attribute is the break-out this
  // file already carries several comments about. createElement/textContent
  // has no HTML-parsing step at all, and the nick is kept in .dataset rather
  // than in the markup, the same way the file checkboxes carry theirs.
  function renderFilelistsSwitcher(rows) {
    // The bot list is rebuilt from scratch every FILELISTS_BOTS_POLL_MS, and
    // everything the filter put on it lives in classes on those rows - so
    // without the restore at the end of this function the greying, the
    // crossed-out names and the operator's own switched-off choices all
    // vanished four seconds after they appeared, repeatedly. The scroll
    // position went with them, which on a channel with thirty-odd
    // advertisers means the list jumps back to the top while being read.
    var keptScroll = el.filelistsBotList.scrollTop;
    var keptFocus = document.activeElement;
    var refocusBot = (keptFocus && keptFocus.classList
      && keptFocus.classList.contains("bot-row"))
      ? keptFocus.dataset.bot : null;

    var list = el.filelistsBotList;
    var previous = state.filelistsSource || "__own__";

    state.filelistsBots = {};
    list.innerHTML = "";

    // OUR OWN LISTS COME FROM THE SERVER NOW, one row each, rather than a
    // single hard-coded row here. An operator who builds a second list
    // through the dashboard could not find it afterwards: the page offered
    // to make a thing and then would not show it. Only the server knows how
    // many lists there are and what they are called.
    //
    // GROUPED BY NICK (#399), one sidebar row per BOT rather than per list.
    // The server still returns one flat row per list - "<nick>/<marker>" for
    // everything but a bot's main list - because that shape is also what
    // /api/filelists/search answers against, and duplicating it here would
    // be a second thing to keep in step. Two bots' own lists (Main, video)
    // never collide with this: own_list_source() gives each its own nick, so
    // grouping never merges them - only a FETCHED bot's own several lists
    // group under its one nick.
    var groupOrder = [];
    var groupsByNick = {};
    rows.forEach(function (row) {
      state.filelistsBots[row.bot] = row;
      var nickKey = String(row.nick || row.bot).toLowerCase();
      var group = groupsByNick[nickKey];
      if (!group) {
        group = { nick: row.nick || row.bot, entries: [] };
        groupsByNick[nickKey] = group;
        groupOrder.push(nickKey);
      }
      group.entries.push(row);
    });
    groupOrder.forEach(function (nickKey) {
      list.appendChild(botRow(groupsByNick[nickKey]));
    });

    // A source that has gone - the bot dropped out of the registry, or its
    // list was removed - falls back to our own rather than leaving the view
    // pointed at nothing.
    var stillThere = rows.some(function (row) {
      return row.bot === previous && row.held;
    });
    state.filelistsSource = stillThere ? previous : "__own__";
    markFilelistsActiveBot();

    // Put back what the rebuild just discarded.
    if (state.filelistsFilterPayload) {
      applyFilterHighlight(state.filelistsFilterPayload);
    }
    el.filelistsBotList.scrollTop = keptScroll;
    if (refocusBot) {
      // Found by WALKING the rows and comparing .dataset, not by building a
      // selector with the nick in it. A nick is remote input, and this file's
      // rule is that one never gets concatenated into anything that is then
      // parsed. The XSS guards in test_webserver.py scan for a data attribute
      // being opened in a concatenation and do not care whether the result is
      // markup or a selector - which is the right amount of strict, and is
      // why this walks instead.
      var candidates = el.filelistsBotList.querySelectorAll(".bot-row");
      for (var r = 0; r < candidates.length; r++) {
        if (candidates[r].dataset.bot === refocusBot) {
          candidates[r].focus();
          break;
        }
      }
    }
  }

  // The list a group's own row speaks for when nothing else has been picked
  // yet - its main list where there is one, the first list otherwise (an
  // own list, and every not-held advert-only row, always has exactly one
  // entry and IS that entry either way).
  function primaryEntry(group) {
    for (var i = 0; i < group.entries.length; i++) {
      if (!group.entries[i].list) { return group.entries[i]; }
    }
    return group.entries[0];
  }

  function botRow(group) {
    var primary = primaryEntry(group);
    var grouped = group.entries.length > 1;

    var button = document.createElement("button");
    button.type = "button";
    button.className = "bot-row";
    // ALWAYS THE PRIMARY LIST'S KEY, never "whichever tab is open" - the row
    // itself does not track which of a bot's lists is currently showing;
    // renderFilelistsTabs() and markFilelistsActiveBot() answer that by NICK
    // instead (see both), which is what lets this stay a single, simple
    // value rather than something that has to be kept in sync on every tab
    // click.
    button.dataset.bot = primary.bot;
    // The NICK as well as the identity. They are the same for a bot's main
    // list and differ for every other one, and the fetch box wants the nick.
    button.dataset.nick = group.nick;
    button.dataset.held = primary.held ? "yes" : "no";

    // TWO SIGNALS, TWO PLACES. The dot used to carry the list's freshness,
    // which left presence - the thing that decides whether asking is worth
    // anything at all - shown nowhere. A list can be perfectly current from a
    // bot that signed off an hour ago.
    //
    // The dot is now whether they are HERE, and the name's colour is what we
    // hold from them. Asked for exactly that way in the beta.
    var led = document.createElement("span");
    led.className = "led " + presenceClass(primary.online);
    led.title = presenceTitle(primary.online);
    button.appendChild(led);

    var name = document.createElement("span");
    name.className = "bot-row-name " + freshnessClass(primary.freshness);
    name.title = ledTitle(primary);
    // grouped: the bare nick, since "label" is a per-LIST string ("SomeBot -
    // rar") that a row now representing several lists at once cannot use
    // without picking one of them to speak for all. Every ungrouped row -
    // which is every own list, and the ordinary case for a fetched one -
    // keeps its existing label untouched, own lists included ("Our own
    // list", or the list's own name).
    name.textContent = grouped ? group.nick : (primary.label || primary.bot);
    button.appendChild(name);

    if (grouped) {
      var badge = document.createElement("span");
      badge.className = "bot-row-lists-badge";
      badge.textContent = String(group.entries.length);
      badge.title = group.entries.length + " lists - open this bot to switch between them.";
      button.appendChild(badge);
    }

    var count = document.createElement("span");
    count.className = "bot-row-count";
    // An em dash, not 0: a bot that published no count did not say, and
    // saying zero would be a claim they never made. The PRIMARY's count,
    // not a sum across lists - a RAR list packs the same albums the main
    // list already counts, so adding the two would not be a real total.
    count.textContent = primary.count === undefined || primary.count === null
      ? "\u2014" : Number(primary.count).toLocaleString();
    button.appendChild(count);

    if (!primary.held && !isOwnSource(primary.bot)) {
      button.title = "You have not downloaded this bot's list. " +
        "Click to put its nick in the fetch box.";
    }
    return button;
  }

  // OURS, WHICHEVER OF OURS. "__own__" alone still means the primary - the
  // meaning GET /api/filelists has always had - and each further served list
  // is "__own__:<name>". Every place that used to compare against the bare
  // string now asks this instead, because missing one would leave a second
  // list looking like a foreign bot: fetchable, refetchable, and offered a
  // Download button for a list we wrote ourselves.
  function isOwnSource(source) {
    var text = String(source || "");
    return text === "__own__" || text.indexOf("__own__:") === 0;
  }

  // A FETCHED SOURCE IS NOT ALWAYS JUST A NICK. A bot's main list keeps the
  // bare nick it has always had; its other lists are "<nick>/<marker>". Every
  // action that concerns the BOT rather than the list - re-fetching, packing a
  // folder, putting a nick in the fetch box - needs the nick out of it, and
  // sending "<nick>/<marker>" to any of them would address a bot that does
  // not exist.
  function splitFetchedSource(source) {
    var text = String(source || "");
    var cut = text.indexOf("/");
    return cut < 0
      ? { nick: text, list: "" }
      : { nick: text.slice(0, cut), list: text.slice(cut + 1) };
  }

  // Which BOT a source key belongs to (#399), for everything that used to
  // compare two source keys for equality and now has to ask "same bot" -
  // several sources ("d_f_d", "d_f_d/rar") can be one grouped sidebar row.
  // Works for an own source too: splitFetchedSource() only splits on "/",
  // own_list_source()'s "__own__:<name>" has none, so it comes back whole -
  // exactly what group.nick already carries for it (see renderFilelistsSwitcher).
  function nickOfSource(source) {
    return splitFetchedSource(source).nick;
  }

  // Every row currently held for one bot, wherever state.filelistsBots put
  // them - not a separate map kept in step by hand, so it can never disagree
  // with what the sidebar last rendered from the same rows.
  function entriesForNick(nick) {
    var nickLower = String(nick || "").toLowerCase();
    var out = [];
    Object.keys(state.filelistsBots).forEach(function (key) {
      var row = state.filelistsBots[key];
      if (String(row.nick || row.bot || "").toLowerCase() === nickLower) {
        out.push(row);
      }
    });
    return out;
  }

  // The ?list= for a source key, or "" for the primary.
  function ownListParam(source) {
    var text = String(source || "");
    return text.indexOf("__own__:") === 0 ? text.slice("__own__:".length) : "";
  }

  // WHETHER THEY ARE HERE. `null` is not "offline" - it is a bot that has not
  // finished joining, where the membership mirror is empty and every nick
  // would read as gone.
  function presenceClass(online) {
    if (online === true) { return "is-online"; }
    if (online === false) { return "is-offline"; }
    return "is-presence-unknown";
  }

  function presenceTitle(online) {
    if (online === true) { return "In a channel with this bot now"; }
    if (online === false) {
      return "Not in any channel this bot is in - a request would go nowhere";
    }
    return "Cannot tell yet - still joining";
  }

  // WHAT WE HOLD FROM THEM, on the name rather than the dot.
  function freshnessClass(freshness) {
    if (freshness === "current" || freshness === "own") { return "is-current"; }
    if (freshness === "changed") { return "is-changed"; }
    if (freshness === "not_held") { return "is-not-held"; }
    return "is-unknown";
  }

  // WHAT THE LED IS COMPARING, not just its verdict.
  //
  // From a maintainer: "redownloaded [a bot's] list, its yellow, but it does
  // not update to green." The tooltip said only "Their list has changed since
  // you downloaded it", which cannot tell you whether the re-download never
  // landed, landed and was refused, or landed fine while the bot advertised
  // something newer again in between. All three look identical from outside,
  // and telling them apart meant reading the daemon log.
  //
  // The payload has carried `advert_then` and `advert_now` since the LED was
  // built - the exact two values webserver._freshness() decides on - and this
  // was dropping them. renderFilelistsFreshness() below already spells them
  // out, but only for the bot currently SELECTED in the List Browser; the LED
  // is what you look at when scanning the bot list itself, which is where the
  // question gets asked. Same describeAdvert() for both, so the two can never
  // drift into telling different stories about one row.
  //
  // Set as a PROPERTY, never concatenated into an attribute: these strings
  // come off another bot's advert, and escapeHtml() encodes & < > and leaves
  // a double quote alone. Same rule as the nick beside it.
  function ledTitle(row) {
    var freshness = row.freshness;
    if (freshness === "changed") {
      return "Their list has changed since you downloaded it. " +
             "They advertised " + describeAdvert(row.advert_then || {}) +
             " when you downloaded it, and now advertise " +
             describeAdvert(row.advert_now || {}) + ".";
    }
    if (freshness === "not_held") {
      return "Not downloaded. They advertise " +
             describeAdvert(row.advert_now || {}) + ".";
    }
    if (freshness === "unknown") {
      return "Cannot tell - we have not seen what they advertise, " +
             "or they publish no date or count";
    }
    if (freshness === "own") { return "Your own list"; }
    return "Current - they still advertise " +
           describeAdvert(row.advert_now || {}) + ".";
  }

  function markFilelistsActiveBot() {
    // BY NICK, not by exact key (#399): the open list can be a bot's RAR or
    // VIDEO list, whose row shows the bot's PRIMARY key in dataset.bot - the
    // row still has to read as "active" for any of its own bot's lists, not
    // only its primary one.
    var openNick = nickOfSource(state.filelistsSource).toLowerCase();
    var rows = el.filelistsBotList.querySelectorAll(".bot-row");
    for (var i = 0; i < rows.length; i++) {
      var active = String(rows[i].dataset.nick || "").toLowerCase() === openNick;
      rows[i].classList.toggle("is-active", active);
      // aria-current, not aria-selected: these are ordinary buttons the
      // operator tabs through, not the options of a listbox, and claiming
      // the listbox role would promise arrow-key navigation we do not
      // implement.
      if (active) {
        rows[i].setAttribute("aria-current", "true");
      } else {
        rows[i].removeAttribute("aria-current");
      }
    }
    renderFilelistsTabs();
  }

  // ONE TAB PER LIST THE OPEN BOT PUBLISHES (#399), shown above the table in
  // place of the second, third sidebar row a bot with more than one list
  // used to need. Reads a list's own marker straight off `list` - "RAR",
  // "VIDEO", whatever an OmenServe-family bot's own list is actually called -
  // rather than the sidebar's "label" field, which is built for a single row
  // ("SomeBot - rar") and was never meant to be split back apart into a name a
  // tab strip can reuse.
  function renderFilelistsTabs() {
    var container = el.filelistsListTabs;
    if (!container) { return; }

    var entries = entriesForNick(nickOfSource(state.filelistsSource));
    if (entries.length < 2) {
      container.hidden = true;
      container.innerHTML = "";
      return;
    }

    // The primary list first, then the rest alphabetically by marker - the
    // same order the server already returns them in (see
    // build_fetched_bot_list_summaries()), kept here rather than trusted,
    // since this function does not know whether that order survived
    // whatever put these rows into state.filelistsBots.
    entries.sort(function (a, b) {
      var listA = String(a.list || "");
      var listB = String(b.list || "");
      if (listA === listB) { return 0; }
      if (!listA) { return -1; }
      if (!listB) { return 1; }
      return listA.toLowerCase() < listB.toLowerCase() ? -1 : 1;
    });

    container.innerHTML = "";
    entries.forEach(function (entry) {
      var tab = document.createElement("button");
      tab.type = "button";
      tab.className = "filelists-list-tab";
      tab.dataset.bot = entry.bot;
      // "Main" for the bare list, never the raw empty string - a tab with no
      // text is not a tab an operator can click on purpose.
      tab.textContent = entry.list ? entry.list : "Main";
      var active = entry.bot === state.filelistsSource;
      tab.classList.toggle("is-active", active);
      if (active) {
        tab.setAttribute("aria-current", "true");
      }
      container.appendChild(tab);
    });
    container.hidden = false;
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
      // "Folders 1-1 of 1 (11,232 files)" is true and says nothing: a flat
      // list has one group because everything is in it, not because the page
      // is showing one folder out of several.
      var caption = (total === 1 && state.filelistsFlat)
        ? files.toLocaleString() + (files === 1 ? " file" : " files")
        : "Folders " + start.toLocaleString() + "–" + end.toLocaleString() +
          " of " + total.toLocaleString() +
          " (" + files.toLocaleString() + (files === 1 ? " file)" : " files)");
      // WHY THIS PAGE IS SHORT (#477).
      //
      // Reported live, with screenshots: pages of 31 folders, then 1, then
      // 148, and an operator reasonably reading it as a broken pager. It is
      // not - page_folder_groups() stops a page early once the folders on it
      // would carry more ROWS than the response ceiling allows, and always
      // returns at least one folder even when that folder alone is over it.
      // "Folders 32–32 of 3 605" is a correct sentence that looks exactly
      // like a bug.
      //
      // The server has computed and sent `row_capped` all along, precisely so
      // this could be said; nothing ever read it. A mechanism that works and
      // cannot explain itself costs more than one that is simply missing,
      // because the operator goes looking for the fault.
      if (state.filelistsRowCapped) {
        caption += " — fewer folders on this page: one or more of them holds " +
          "enough files to reach the per-page limit on its own. " +
          "Next shows the rest.";
      }
      el.filelistsPageInfo.textContent = caption;
      el.filelistsPrevBtn.disabled = offset <= 0;
      el.filelistsNextBtn.disabled = (offset + shown) >= total;
    }

    // A file that reached us with no folder heading above it still has to be
    // shown under something.
    function folderLabel(name) {
      return name ? name : "(no folder)";
    }

    // A LIST WITH NO FOLDERS IN IT AT ALL. A bot that publishes its albums as
    // packs has one row per pack and no directory structure to speak of, so
    // the whole list arrives as a single unnamed group - see
    // folderGroupsFrom(), which is also what a flat .txt list produces.
    //
    // Reported from the beta: "If a file list is a rar file list there are no
    // folders. When I click on a rar file list I see 1 folder that I have to
    // expand. That's not needed when there are no folders." Quite right: it
    // was one collapsed row reading "(no folder) - 11,232 files", and a click
    // to get past a grouping that groups nothing.
    //
    // The test is the list's own shape rather than anything about .rar: one
    // group, and that group unnamed. A list that genuinely has one folder is
    // NOT flat - it has a name worth showing, and its heading says which
    // folder the rows below belong to.
    function listIsFlat(groups) {
      return groups.length === 1 && !groups[0].folder;
    }

    // The count is the point of a collapsed folder: it says how much is inside
    // before the operator spends a click finding out. `count` is the folder's
    // TRUE size, so a folder that arrived truncated still reports what it
    // holds rather than only what fitted on the page.
    // A file is only fetchable when it belongs to someone ELSE's list -
    // browsing our own is direct filesystem access already, and
    // /api/fetch/enqueue exists to reach another bot over IRC, not this one.
    //
    // While FILTERING there is no single source: the rows come from every
    // list held, and every one of them is another bot's by definition.
    //
    // ONE FUNCTION, because the folder heading and the file rows have to
    // agree. A heading offering a "select everything here" box over rows with
    // no checkboxes to select is worse than either alone.
    function rowsAreFetchable() {
      return (state.filelistsFilter || "").trim()
        ? true
        : !isOwnSource(state.filelistsSource || "__own__");
    }

    // "2 files" means two DIFFERENT things in the two views, and they look
    // identical. Browsing, it is how big the folder is. Filtering, the group
    // holds only the rows that matched - so a nine-track album whose title
    // matched twice read as an album with two tracks in it, which is what it
    // was reported as.
    function folderCountNoun(count) {
      if ((state.filelistsFilter || "").trim()) {
        return count === 1 ? " match" : " matches";
      }
      return count === 1 ? " file" : " files";
    }

    // WHERE THE MATCH IS, in a title that is often a hundred characters of
    // artist, album, year, encoder and track number. Asked for in the beta
    // alongside the phrase change: finding the row is half the job, and the
    // other half is seeing why it is a row at all.
    //
    // Marks what was TYPED, not what FTS5 matched: the last phrase is a
    // prefix, so typing "amar" matches "Amarth" - and highlighting the four
    // characters the operator put in is the honest reading of "highlight the
    // matched characters".
    //
    // EVERY PIECE ESCAPED SEPARATELY. The title comes off another bot's list.
    // escapeHtml() is the same treatment it already had; the only change is
    // that it is applied to three pieces instead of one, so the <mark> can go
    // between them without the title ever being parsed as markup.
    function highlightedTitle(title) {
      var text = String(title || "");
      var segments = state.filelistsMatchTerms || [];
      if (!segments.length) { return escapeHtml(text); }

      var lower = text.toLowerCase();
      var ranges = [];
      for (var s = 0; s < segments.length; s++) {
        var needle = String(segments[s] || "").toLowerCase();
        if (!needle) { continue; }
        var at = lower.indexOf(needle);
        while (at !== -1) {
          ranges.push([at, at + needle.length]);
          at = lower.indexOf(needle, at + needle.length);
        }
      }
      if (!ranges.length) { return escapeHtml(text); }

      // Merged, because two phrases can overlap in one title - and nesting a
      // <mark> inside another renders as a darker patch that reads like a
      // third kind of match.
      ranges.sort(function (a, b) { return a[0] - b[0]; });
      var merged = [ranges[0]];
      for (var i = 1; i < ranges.length; i++) {
        var last = merged[merged.length - 1];
        if (ranges[i][0] <= last[1]) {
          last[1] = Math.max(last[1], ranges[i][1]);
        } else {
          merged.push(ranges[i]);
        }
      }

      var out = "";
      var cursor = 0;
      for (var m = 0; m < merged.length; m++) {
        out += escapeHtml(text.slice(cursor, merged[m][0]));
        out += "<mark class=\"filter-hit\">" +
               escapeHtml(text.slice(merged[m][0], merged[m][1])) + "</mark>";
        cursor = merged[m][1];
      }
      return out + escapeHtml(text.slice(cursor));
    }

    function folderHeadingHtml(group, index) {
      var count = group.count || 0;
      // SELECT THE WHOLE FOLDER. Asked for during the beta: an album is the
      // unit people actually want, and ticking nine boxes one at a time to
      // get one is the kind of work a page should be doing for them.
      //
      // In the same column as the file checkboxes it commands, so the
      // relationship is visible rather than something to work out - which is
      // why the heading's cell is split rather than left spanning all five.
      //
      // It selects the rows that are RENDERED. A folder past the page's row
      // ceiling arrives cut short and says so in its own row (see
      // folderFilesHtml), and a box that silently claimed the rest would be
      // claiming to have queued files nobody has seen.
      //
      // data-folder-index is safe to string-concatenate: it is this group's
      // own position in the internal `groups` array, not untrusted content.
      var checkCell = rowsAreFetchable()
        ? "<td class=\"col-check\"><input type=\"checkbox\"" +
          " class=\"filelists-folder-check\" data-folder-index=\"" + index +
          "\" title=\"Select every file in this folder\"" +
          " aria-label=\"Select every file in this folder\"></td>"
        : "<td class=\"col-check\"></td>";
      return "<tr class=\"folder-row\">" +
        checkCell +
        "<td colspan=\"4\">" +
          "<button type=\"button\" class=\"folder-toggle\" aria-expanded=\"false\"" +
                 " data-folder-index=\"" + index + "\">" +
            "<span class=\"folder-caret\" aria-hidden=\"true\"></span>" +
            "<span class=\"folder-name\">" +
              escapeHtml(folderLabel(group.folder)) + "</span>" +
            "<span class=\"folder-count\">" + count.toLocaleString() +
              folderCountNoun(count) + "</span>" +
          "</button>" +
        "</td></tr>";
    }
    function folderFilesHtml(group, index, flat) {
      var entries = group.entries || [];
      // A file is only fetchable when it belongs to someone ELSE's list -
      // browsing our own is direct filesystem access already, and
      // /api/fetch/enqueue exists to reach another bot over IRC, not this one.
      //
      // While FILTERING there is no single source: the rows come from every
      // list held, and every one of them is another bot's by definition. This
      // asked only about the selected source, which defaults to our own list -
      // so filtering before picking a bot rendered every result with no
      // checkbox and no way to queue any of it.
      var fetchable = rowsAreFetchable();
      var rows = entries.map(function (row, position) {
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
        // Two states, never three: "requested" while it is still in
        // flight, "received" once it has arrived, and nothing at all for a
        // failed one - a failure is not a thing you have, and marking it
        // would discourage the one useful action left, which is to ask
        // again. The server decides which; this only renders it.
        //
        // escapeHtml() on the mark as well, even though it comes from a
        // fixed set: the day it does not, this line should already be safe.
        var mark = row.mark === "received" || row.mark === "requested"
          ? " <span class=\"file-mark is-" + row.mark + "\">" +
            escapeHtml(row.mark === "received" ? "have it" : "asked") +
            "</span>"
          : "";
        // A ROW THAT ASKS FOR A FOLDER, not a file. A bot that packs albums
        // publishes a list whose every row is the line to type - "!<nick>
        // !rar <folder>" - so the list itself says, per folder, what that bot
        // will pack. Nothing is inferred from an advert or guessed at from a
        // filename convention, and the two lists need not agree: a bot can
        // offer one folder as loose files and another only as a pack.
        //
        // data-folder-index is this group's own position in the internal
        // array and is safe to concatenate; the FOLDER is not - it is a path
        // out of a foreign bot's list - so it goes on via .dataset in
        // attachFilelistsFolderRarData(), like the bot nick beside it.
        var rarCell = (row.rar_folder && fetchable)
          ? "<button type=\"button\" class=\"btn btn-small folder-rar-btn\"" +
            " data-folder-index=\"" + index + "\" data-entry-index=\"" + position +
            "\">Get folder as .rar</button>"
          : "";
        // Hidden and indented only when there is a heading to hide them
        // under. In a flat list they ARE the table.
        return "<tr class=\"file-row" + (flat ? "" : " is-hidden") +
          "\" data-folder-index=\"" + index + "\">" +
          checkCell +
          "<td class=\"col-mono" + (flat ? "" : " col-indent") + "\">" +
          highlightedTitle(row.title) + mark +
          (rarCell ? " " + rarCell : "") + "</td>" +
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
          "<tr class=\"file-row folder-truncated" + (flat ? "" : " is-hidden") +
          "\" data-folder-index=\"" + index + "\"><td colspan=\"5\">" +
          "Showing the first " + entries.length.toLocaleString() + " of " +
          (group.count || 0).toLocaleString() +
          (flat ? " files in this list." : " files in this folder.") +
          "</td></tr>");
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
      var target = evt.target;
      if (!target.classList) { return; }
      if (target.classList.contains("filelists-folder-check")) {
        setFolderChecked(target.dataset.folderIndex, target.checked);
        updateFilelistsDownloadSelectedState();
        return;
      }
      if (target.classList.contains("filelists-check")) {
        state.filelistsLastChecked = target;
        // The folder box follows its files, so it cannot claim the folder is
        // selected while one row in it is not.
        syncFolderCheck(target.closest("tr") &&
                        target.closest("tr").dataset.folderIndex);
        updateFilelistsDownloadSelectedState();
      }
    });

    // Reported live: ticking the select-all box in a FLAT list's table head
    // did nothing to the rows below it. renderFlatListControls() moves that
    // checkbox into el.filelistsHeadCheck - a <th>, a sibling of
    // el.filelistsBody rather than something inside it - and the listener
    // just above is delegated on filelistsBody alone, so a change event
    // starting in the head never reached it. Same handler, attached to the
    // one other place a ".filelists-folder-check" can appear; its own
    // innerHTML is rebuilt exactly like the body's rows are, for the same
    // reason a listener belongs on the stable parent instead of the
    // checkbox itself.
    el.filelistsHeadCheck.addEventListener("change", function (evt) {
      var target = evt.target;
      if (!target.classList || !target.classList.contains("filelists-folder-check")) {
        return;
      }
      setFolderChecked(target.dataset.folderIndex, target.checked);
      updateFilelistsDownloadSelectedState();
    });

    // TICK EVERY FILE IN ONE FOLDER, including the rows of a COLLAPSED one.
    // They are in the document already - collapsing hides them rather than
    // removing them - so a folder can be selected without being opened, which
    // is most of the point when a list has hundreds of them.
    function folderBoxes(index) {
      if (index === undefined || index === null || index === "") { return []; }
      // The index is our own loop counter, but it still reaches a selector -
      // so it is matched by walking rather than concatenated into one, the
      // same rule the bot rows follow.
      var rows = el.filelistsBody.querySelectorAll("tr.file-row");
      var found = [];
      for (var i = 0; i < rows.length; i++) {
        if (rows[i].dataset.folderIndex !== String(index)) { continue; }
        var box = rows[i].querySelector(".filelists-check");
        if (box) { found.push(box); }
      }
      return found;
    }

    function setFolderChecked(index, checked) {
      var boxes = folderBoxes(index);
      for (var i = 0; i < boxes.length; i++) {
        boxes[i].checked = checked;
      }
      // The shift-range anchor is "the last box the operator actually
      // touched". A folder box is not one of those, and leaving a stale
      // anchor would make the next shift-click extend from a row nobody
      // pointed at.
      state.filelistsLastChecked = null;
    }

    // Checked, unchecked, or INDETERMINATE for some-but-not-all. The third
    // state is the honest one: a box that showed "unchecked" while four of
    // nine rows were selected would be describing a selection that is not
    // the one in force.
    function syncFolderCheck(index) {
      var box = folderCheckFor(index);
      if (!box) { return; }
      var boxes = folderBoxes(index);
      var checked = 0;
      for (var i = 0; i < boxes.length; i++) {
        if (boxes[i].checked) { checked++; }
      }
      box.checked = boxes.length > 0 && checked === boxes.length;
      box.indeterminate = checked > 0 && checked < boxes.length;
    }

    function folderCheckFor(index) {
      if (index === undefined || index === null || index === "") { return null; }
      // A flat list's own folder-check lives in the table HEAD, not the body
      // (see renderFlatListControls()) - checked first since a flat list has
      // no folder heading in the body to find one under anyway.
      var head = el.filelistsHeadCheck
        && el.filelistsHeadCheck.querySelector(".filelists-folder-check");
      if (head && head.dataset.folderIndex === String(index)) { return head; }
      var boxes = el.filelistsBody.querySelectorAll(".filelists-folder-check");
      for (var i = 0; i < boxes.length; i++) {
        if (boxes[i].dataset.folderIndex === String(index)) { return boxes[i]; }
      }
      return null;
    }

    function syncEveryFolderCheck() {
      var boxes = el.filelistsBody.querySelectorAll(".filelists-folder-check");
      for (var i = 0; i < boxes.length; i++) {
        syncFolderCheck(boxes[i].dataset.folderIndex);
      }
    }

    // SHIFT EXTENDS A RANGE (#133). Handled on click rather than change,
    // because the range has to be computed against the state BEFORE the
    // browser toggles the clicked box - and because shift-clicking a checkbox
    // also selects text across the rows it spans, which looks broken.
    //
    // The anchor is the last box the operator actually touched, which is what
    // every file manager means by it. Ctrl needs no code at all: a checkbox
    // toggles one box on its own, which is already ctrl's behaviour.
    //
    // A rebuild of the table drops the anchor (the element is gone), so
    // renderFilelists clears it rather than leaving a reference to a node
    // that is no longer in the document.
    el.filelistsBody.addEventListener("click", function (evt) {
      var box = evt.target;
      if (!box.classList || !box.classList.contains("filelists-check")) { return; }
      var anchor = state.filelistsLastChecked;
      if (!evt.shiftKey || !anchor || anchor === box) { return; }

      var boxes = Array.prototype.slice.call(
        el.filelistsBody.querySelectorAll(".filelists-check"));
      var from = boxes.indexOf(anchor);
      var to = boxes.indexOf(box);
      if (from === -1 || to === -1) { return; }

      evt.preventDefault();
      window.getSelection && window.getSelection().removeAllRanges();

      // The clicked box takes the anchor's state, and so does everything
      // between them - "extend the selection to here", not "toggle each".
      var wanted = anchor.checked;
      if (from > to) { var swap = from; from = to; to = swap; }
      for (var i = from; i <= to; i++) {
        boxes[i].checked = wanted;
      }
      // A range can span several folders, so every heading is re-read rather
      // than only the one clicked in.
      syncEveryFolderCheck();
      updateFilelistsDownloadSelectedState();
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
      // The old anchor pointed into the table that has just been replaced.
      state.filelistsLastChecked = null;
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
        // The GROUP's bot when it has one. A cross-list filter shows
        // groups from several bots at once, and state.filelistsSource is
        // whichever list the sidebar has selected - not the one this folder
        // came from. Requesting the right folder from the wrong bot is a
        // request that cannot succeed.
        button.dataset.bot = splitFetchedSource(
          group.bot || state.filelistsSource).nick;
        // THE ROW'S OWN FOLDER, taken from the request line that put the
        // button there - not the folder HEADING the row happens to sit under.
        // A RAR list's rows are grouped under whatever heading that list
        // carries, which is not the folder being asked for.
        var position = parseInt(button.dataset.entryIndex, 10);
        var entries = group.entries || [];
        var row = isNaN(position) ? null : entries[position];
        button.dataset.folder = (row && row.rar_folder) || "";
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

    if (el.filelistsPurgeListBtn) {
      el.filelistsPurgeListBtn.addEventListener("click", purgeCurrentList);
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
          // Re-read the page so the rows just queued say so. The marks are
          // stamped server-side when a page is built, so without this they
          // would not appear until something else caused a reload - and the
          // one moment an operator most wants to see "asked" is immediately
          // after asking.
          loadFilelists();
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

    // Their advert THEN against their advert NOW - never against our own
  // parsed row count, which is a different thing counted a different way (see
  // webserver._freshness). "unknown" renders as nothing at all: a bot that
  // publishes no date, or one whose advert we have not seen, should show no
  // freshness claim rather than an invented one.
  // TAKES THE BOTS WITH NOTHING TO SHOW OFF THE SCREEN, and clears every mark
  // again when the term goes. Driven by what the server said rather than by
  // what came back in the page: the page is capped, so a bot whose matches
  // all fall past the cap would look empty when it is not.
  //
  // THEY USED TO BE DIMMED, and the argument for that was written into the
  // stylesheet: the sidebar is also the answer to "who has this", and a row
  // that vanished would take that answer with it. That argument was right
  // about what matters and wrong about what to do. Asked for in the beta:
  // "names get hidden as you type something that you search lists for. only
  // the names with result are shown."
  //
  // The answer is kept, and made easier to read than it was. Dimming asked
  // the operator to scan thirty rows and judge opacity; a count states it -
  // "12 lists with no match hidden" - and the button beside it puts them
  // back, dimmed, for anyone who wants to look. So the row that vanished
  // gives its answer as a number instead of as an absence.
  //
  // FOUR ROWS ARE NEVER HIDDEN, and none of them is a special case for its
  // own sake:
  //   - our own list, which this filter does not search at all;
  //   - the list currently open, or the table would be showing a list with
  //     no row;
  //   - the row holding keyboard focus, because hiding it drops focus to the
  //     body and loses the operator's place;
  //   - all of them, once the operator has asked to see them.
  function applyFilterHighlight(payload) {
    var rows = el.filelistsBotList.querySelectorAll(".bot-row");
    var filtering = !!(state.filelistsFilter || "").trim();
    var empty = {};
    if (payload && Array.isArray(payload.empty)) {
      payload.empty.forEach(function (name) { empty[String(name).toLowerCase()] = true; });
    }
    var hidden = 0;
    for (var i = 0; i < rows.length; i++) {
      var row = rows[i];
      var bot = String(row.dataset.bot || "").toLowerCase();
      var nick = String(row.dataset.nick || "").toLowerCase();
      // GROUP-WIDE (#399): a row now answers for every list its bot has, so
      // it reads as having "nothing" only if NONE of them matched - one
      // matching list is reason enough to keep the row on screen, even
      // though the tab open on it right now might be a different, empty one.
      var group = entriesForNick(nick);
      var groupKeys = group.length
        ? group.map(function (entry) { return String(entry.bot).toLowerCase(); })
        : [bot];
      // Our own list is not one of the lists the filter searches - it covers
      // lists FETCHED from other bots - so it is never marked by it.
      var nothing = filtering && !isOwnSource(bot)
        && groupKeys.every(function (key) { return empty[key] === true; });
      var pinned = nick === String(nickOfSource(state.filelistsSource) || "").toLowerCase()
        || row.contains(document.activeElement);
      var away = nothing && !pinned && !state.filelistsRevealEmpty;
      if (away) { hidden += 1; }
      row.classList.toggle("is-filtered-away", away);
      // Still dimmed when it cannot be hidden, or when the operator has asked
      // to see them: the row is on screen and the fact that it has nothing is
      // still the thing worth knowing about it.
      row.classList.toggle("is-filtered-out", nothing && !away);
      // Switched off BY THE OPERATOR, which is a different thing from having
      // nothing to show and reads differently: one is an answer, the other is
      // a choice, and the choice is reversible by clicking again. Keyed by
      // nick, same as the toggle that sets it - see the sidebar click handler.
      row.classList.toggle(
        "is-excluded",
        filtering && !!state.filelistsExcluded[nick]);
    }

    renderRevealButton(filtering, hidden);

    if (!filtering) {
      el.filelistsFilterStatus.hidden = true;
      el.filelistsFilterStatus.textContent = "";
      return;
    }
    var files = (payload && payload.total_files) || 0;
    var matched = (payload && payload.matched && payload.matched.length) || 0;
    var text = files
      ? files.toLocaleString() + (payload && payload.truncated ? "+" : "") +
        " match" + (files === 1 ? "" : "es") + " in " + matched +
        " list" + (matched === 1 ? "" : "s")
      : "No matches in any list you hold";
    if (payload && payload.truncated) {
      text += " \u2014 showing the first " + files + ", narrow the term to see the rest";
    }
    el.filelistsFilterStatus.hidden = false;
    el.filelistsFilterStatus.textContent = text;
  }

  // WHAT WAS TAKEN AWAY, and the way back to it. A count rather than nothing
  // at all, because a sidebar that quietly holds a different number of rows
  // each keystroke is one the operator cannot trust to answer "who has this".
  function renderRevealButton(filtering, hidden) {
    var button = el.filelistsFilterReveal;
    if (!button) { return; }
    if (!filtering || (!hidden && !state.filelistsRevealEmpty)) {
      button.hidden = true;
      return;
    }
    button.hidden = false;
    button.textContent = state.filelistsRevealEmpty
      ? "Hide lists with no match"
      : "Show " + hidden + " with no match";
  }

  // Every group the current answer holds, minus the bots switched off.
  // Client-side on purpose: #133 calls the toggling trivial precisely because
  // the rows are already in the browser, and re-asking the server for a
  // narrower set would be slower than the search that fetched them.
  function visibleFilterGroups(groups) {
    if (!(state.filelistsFilter || "").trim()) { return groups; }
    return groups.filter(function (group) {
      // By nick (#399): group.bot is the exact list ("d_f_d/rar") a search
      // result folder came from, but exclusion is a per-BOT choice made in
      // the sidebar - see the click handler there for why.
      return !state.filelistsExcluded[nickOfSource(group.bot).toLowerCase()];
    });
  }

  function renderFilelistGroups(allGroups, filtering) {
    var groups = visibleFilterGroups(allGroups);
    if (!groups.length) {
      el.filelistsBody.innerHTML = emptyRow(5, filtering
        ? (allGroups.length
            ? "Every list with a match is switched off."
            : "Nothing in any list you hold matches that.")
        : "No files published yet.");
      updateFilelistsDownloadSelectedState();
      return;
    }
    var flat = listIsFlat(groups);
    state.filelistsFlat = flat;
    el.filelistsBody.innerHTML = groups.map(function (group, index) {
      return (flat ? "" : folderHeadingHtml(group, index)) +
        folderFilesHtml(group, index, flat);
    }).join("");
    renderFlatListControls(flat);
    attachFilelistsCheckboxData(groups);
    attachFilelistsFolderRarData(groups);
    updateFilelistsDownloadSelectedState();
  }

  // WITH NO FOLDERS THERE IS NOTHING TO EXPAND, and two buttons that do
  // nothing are worse than none: they say the table has a structure it does
  // not have.
  //
  // The select-all box moves rather than going away. It lives on the folder
  // heading normally - in the same column as the boxes it commands - and a
  // flat list has no heading to carry it, so it goes to the table header,
  // which is where a table-wide select belongs anyway. Same class and the
  // same data-folder-index, so the existing handler needs no changes: every
  // row in a flat list is in group 0.
  function renderFlatListControls(flat) {
    el.filelistsExpandAll.hidden = flat;
    el.filelistsCollapseAll.hidden = flat;

    var head = el.filelistsHeadCheck;
    if (!head) { return; }
    head.innerHTML = (flat && rowsAreFetchable())
      ? '<input type="checkbox" class="filelists-folder-check"' +
        ' data-folder-index="0" title="Select every file shown"' +
        ' aria-label="Select every file shown">'
      : "";
  }

  // Re-renders from the answer already held. Used by the sidebar toggle and
  // by the two buttons, none of which change what MATCHES - only which of
  // the matching lists is on screen.
  function rerenderFromFilterPayload() {
    var payload = state.filelistsFilterPayload;
    if (!payload) { return; }
    applyFilterHighlight(payload);
    renderFilelistGroups(folderGroupsFrom(payload), true);
  }

  function setEveryListShown(shown) {
    state.filelistsExcluded = {};
    if (!shown) {
      var payload = state.filelistsFilterPayload;
      var names = (payload && payload.matched) || [];
      // By nick (#399): `names` are exact list keys ("d_f_d", "d_f_d/rar"),
      // exclusion is per bot - mapping both of one bot's keys onto the same
      // nick here is harmless, it just sets the same flag twice.
      names.forEach(function (name) {
        state.filelistsExcluded[nickOfSource(name).toLowerCase()] = true;
      });
    }
    rerenderFromFilterPayload();
  }

  function runFilelistsFilter() {
    // Every reply carries the token it was issued with, and only the newest
    // is allowed to render. Typing fast puts several requests in flight and
    // the slowest is not necessarily the oldest - without this, stopping
    // typing can leave an earlier term's results on screen under the later
    // term in the box.
    state.filelistsFilterToken += 1;
    var token = state.filelistsFilterToken;
    var term = (state.filelistsFilter || "").trim();

    el.filelistsFilterClear.hidden = !term;
    el.filelistsFilterActions.hidden = !term;
    // A new term is a new question, so nothing carries over: a bot switched
    // off while looking for one thing should not be silently switched off
    // while looking for the next, and rows put back on screen for one term
    // should not still be there, unasked, for the next.
    state.filelistsExcluded = {};
    state.filelistsRevealEmpty = false;
    state.filelistsOffset = 0;
    state.filelistsHistory = [];

    window.setTimeout(function () {
      if (token !== state.filelistsFilterToken) { return; }
      loadFilelists();
    }, term ? FILELISTS_FILTER_DEBOUNCE_MS : 0);
  }

  // #399's follow-up: search the ONE list currently open, server-side, so
  // finding a file in a bot's ten-thousand-row archive does not mean paging
  // through it 200 rows at a time. A different question from
  // runFilelistsFilter() above, which spans every list held and replaces
  // this whole view - this one narrows loadFilelists()'s own request
  // instead (see its "q" handling), so the tabs, the pager and everything
  // else about "which list is open" stay exactly as they are.
  function runFilelistsListQuery() {
    state.filelistsListQueryToken += 1;
    var token = state.filelistsListQueryToken;
    var term = (state.filelistsListQuery || "").trim();

    state.filelistsOffset = 0;
    state.filelistsHistory = [];

    window.setTimeout(function () {
      if (token !== state.filelistsListQueryToken) { return; }
      loadFilelists();
    }, term ? FILELISTS_FILTER_DEBOUNCE_MS : 0);
  }

  // Bumping the token here, not just clearing the field/state, cancels
  // whatever debounced call is already in flight for the list being left -
  // without it, a still-pending timer from the OLD list could fire after
  // the switch and briefly narrow the new one by a term nobody typed there.
  function resetFilelistsListQuery() {
    state.filelistsListQuery = "";
    state.filelistsListQueryToken += 1;
    if (el.filelistsListSearchInput) { el.filelistsListSearchInput.value = ""; }
  }

  function renderFilelistsFreshness() {
    // Same trigger, same question: both read state.filelistsBots for the open
    // source, so anywhere one needs redrawing the other does too.
    renderFilelistsPurge();

    var banner = el.filelistsFreshness;
    if (!banner) { return; }
    var row = state.filelistsBots[state.filelistsSource];
    if (!row || row.freshness !== "changed") {
      banner.hidden = true;
      banner.textContent = "";
      return;
    }

    var then = row.advert_then || {};
    var now = row.advert_now || {};
    banner.hidden = false;
    banner.textContent =
      "Their list has changed since you downloaded it \u2014 they advertised " +
      describeAdvert(then) + ", and now advertise " + describeAdvert(now) +
      ". Fetch it again to see what they are offering now.";
  }

  // OFFERED ONLY WHERE IT MEANS SOMETHING. Your own list is the library and
  // has no fetched copy to remove; a bot you have only seen advertising has
  // nothing downloaded either. A button that is present but errors when
  // pressed teaches people to distrust the whole toolbar.
  function renderFilelistsPurge() {
    var button = el.filelistsPurgeListBtn;
    if (!button) { return; }
    var source = state.filelistsSource || "__own__";
    var row = state.filelistsBots[source];

    button.hidden = isOwnSource(source) || !row || !row.held;
    button.disabled = false;
  }

  function purgeCurrentList() {
    var source = state.filelistsSource;
    var row = state.filelistsBots[source];
    if (!source || isOwnSource(source) || !row || !row.held) { return; }

    // THE BOT, not row.label (#399): the request removes every list that bot
    // has (POST /api/filelists/<source>/purge resolves <nick>/<marker> to
    // its bot and purges the whole thing - one archive is one directory),
    // and the tab open right now might be its RAR or VIDEO list rather than
    // its main one - "Remove everything downloaded from SomeBot - rar?" would
    // undersell exactly what is about to happen.
    var name = row.nick || row.label || row.bot || source;
    // Confirmed because it deletes files and cannot be undone from here -
    // getting the list back means downloading it from that bot again, which
    // needs the bot to still be around.
    if (!window.confirm(
        "Remove everything downloaded from " + name + "?" +
        String.fromCharCode(10, 10) +
        "Every list this bot has, its extracted files and its rows in the " +
        "cross-list search index are all deleted. Fetching from it again " +
        "is the only way back.")) {
      return;
    }

    el.filelistsPurgeListBtn.disabled = true;
    postJson("/api/filelists/" + encodeURIComponent(source) + "/purge", {})
      .then(function (res) {
        if (!res.ok) {
          el.filelistsPurgeListBtn.disabled = false;
          showFilelistsFetchStatus((res.data && res.data.error)
                                  || "Could not purge that list.", true);
          return;
        }
        showFilelistsFetchStatus((res.data && res.data.detail) || "Purged.",
                                false);
        // Back to our own list, because the one that was open no longer
        // exists - leaving it selected would leave the table showing rows
        // from a list that has just been deleted.
        state.filelistsSource = "__own__";
        state.filelistsOffset = 0;
        state.filelistsHistory = [];
        resetFilelistsListQuery();
        pollFilelistsBots();
        loadFilelists();
      });
  }

  function describeAdvert(advert) {
    var parts = [];
    if (advert.files) { parts.push(Number(advert.files).toLocaleString() + " files"); }
    if (advert.list_date) { parts.push("built " + advert.list_date); }
    return parts.length ? parts.join(", ") : "nothing we could read";
  }

  function loadFilelists() {
      // THE REPLY's token, not the timer's. runFilelistsFilter() already had
      // one and its comment claimed "only the newest is allowed to render" -
      // but it was compared inside the debounce callback, BEFORE this
      // function was even called, so it decided which request to send and
      // nothing decided which reply to draw.
      //
      // Two requests are easily in flight at 120ms of debounce: a broad term
      // is slow, one more character is narrow and fast, and the narrow reply
      // arrives first. The broad one then lands and repaints the table and
      // the sidebar under the later term still in the box - and because it
      // also caches itself as filelistsFilterPayload, every later re-render
      // keeps serving it until the next keystroke.
      state.filelistsLoadToken += 1;
      var loadToken = state.filelistsLoadToken;

      el.filelistsBody.innerHTML = emptyRow(5, "Loading…");
      var source = state.filelistsSource || "__own__";
      var offset = state.filelistsOffset || 0;
      var filter = (state.filelistsFilter || "").trim();
      var url;
      if (filter) {
        // The filter replaces the browse view rather than narrowing it: it
        // spans every list held, so "which bot am I looking at" stops being
        // the question while a term is set. The sidebar still shows which
        // bots have matches - see applyFilterHighlight().
        url = "/api/filelists/search?q=" + encodeURIComponent(filter);
      } else {
        var base;
        if (isOwnSource(source)) {
          var listParam = ownListParam(source);
          base = "/api/filelists" + (listParam
            ? "?list=" + encodeURIComponent(listParam) + "&"
            : "?");
        } else {
          // A bot's other lists are "<nick>/<marker>". The nick is a PATH
          // segment and the marker a query parameter, so a marker containing
          // anything path-like cannot reshape the URL - and the route reads
          // exactly the two it is given.
          var parts = splitFetchedSource(source);
          base = "/api/filelists/bot/" + encodeURIComponent(parts.nick)
            + (parts.list ? "?list=" + encodeURIComponent(parts.list) + "&" : "?");
        }
        url = base + "offset=" + offset + "&limit=" + FILELISTS_PAGE_SIZE;
        // #399's follow-up: narrows THIS list's own rows, unlike the
        // sidebar's filter above which replaces the view entirely.
        var listQuery = (state.filelistsListQuery || "").trim();
        if (listQuery) {
          url += "&q=" + encodeURIComponent(listQuery);
        }
      }

      fetchJson(url)
        .then(function (payload) {
          markConnection(true);
          if (loadToken !== state.filelistsLoadToken) { return; }
          state.filelistsLoaded = true;
          var groups = folderGroupsFrom(payload);
          applyFilterHighlight(payload);
          state.filelistsTotal = Array.isArray(payload)
            ? groups.length : (payload.total || 0);
          state.filelistsReturned = groups.length;
          // The server has always sent this and nothing has ever read it
          // (#477). An array payload is the unpaged shape, which cannot be
          // capped.
          state.filelistsRowCapped = Array.isArray(payload)
            ? false : !!payload.row_capped;
          renderFilelistsPager(Array.isArray(payload)
            ? payload.length : (payload.total_files || 0));
          state.filelistsFilterPayload = filter ? payload : null;
          // The pieces the server actually matched on, parsed there rather
          // than here: one parse cannot disagree with itself, and "amon*amar"
          // is two phrases whichever side you split it on.
          state.filelistsMatchTerms = filter ? (payload.terms || []) : [];
          renderFilelistGroups(groups, !!filter);
        })
        .catch(function (err) {
          markConnection(false);
          // A superseded request failing is not this view's problem: the
          // newer one is what the operator is waiting for, and painting an
          // error over its results would be the same staleness bug wearing
          // an error message.
          if (loadToken !== state.filelistsLoadToken) { return; }
          el.filelistsBody.innerHTML = emptyRow(5, "Could not load file lists: " + err.message);
          updateFilelistsDownloadSelectedState();
        });
    }

  // ------------------------------------------- Coming from OmenServe (#69)

  // What the server says it recognises. Fetched once and cached: the filter
  // below has to keep exactly the lines the parser knows about, and hard-
  // coding that list here is how the two would drift - a field added to
  // omenserve_import.FIELDS would then be stripped out before it arrived.
  var importVariableNames = null;

  function importVariables() {
    if (importVariableNames) { return Promise.resolve(importVariableNames); }
    return fetchJson("/api/stats/import/variables").then(function (payload) {
      importVariableNames = (payload && payload.variables) || [];
      return importVariableNames;
    });
  }

  // KEEP ONLY THE COUNTER LINES. A real vars.ini on the install #69 was
  // written from held 280 variables: nicks, channel names, paths, add-on
  // settings and passwords among them. None of that is any of the bot's
  // business, and none of it needs to cross the network for this to work.
  //
  // Done here rather than server-side for exactly that reason: the filtering
  // has to happen before the upload, or it is not filtering.
  function keepOnlyCounterLines(text, names) {
    var wanted = {};
    names.forEach(function (name) { wanted[String(name).toLowerCase()] = true; });
    return String(text || "").split(/\r?\n/).filter(function (line) {
      var match = /^\s*n\d+\s*=\s*(%\S+)/i.exec(line);
      return !!match && wanted[match[1].toLowerCase()] === true;
    }).join("\n");
  }

  function showImportStatus(text, isError) {
    el.importStatus.hidden = !text;
    el.importStatus.textContent = text || "";
    el.importStatus.classList.toggle("is-error", !!isError);
  }

  function resetImportPreview() {
    state.importValues = null;
    el.importPreviewWrap.hidden = true;
    el.importPreview.innerHTML = "";
    el.importWarning.hidden = true;
    el.importWarning.textContent = "";
    el.importConfirm.hidden = true;
  }

  function renderImportPreview(payload) {
    var current = payload.current || {};
    var values = payload.values || {};
    var byTarget = {
      // Two labels share one target (#414): %mx.rarsent (packed/RAR sends)
      // and %sdmpxsent (OmenServe's own plain-file count) both feed
      // total_files, summed server-side before this ever arrives here.
      "Files sent (packed)": "total_files",
      "Files sent (plain)": "total_files",
      "Bytes sent": "total_bytes",
      "Speed record": "speed_record"
    };

    var body = "";
    (payload.rows || []).forEach(function (row) {
      if (row.value === null || row.value === undefined) { return; }
      var target = byTarget[row.label];
      var now = target ? (current[target] || 0) : null;
      var after = target && values[target] !== undefined ? values[target] : null;
      body += "<tr>" +
        "<td>" + escapeHtml(row.label) +
          (target ? "" : " <span class=\"import-skipped\">not imported</span>") +
        "</td>" +
        "<td class=\"col-num col-mono\">" +
          (now === null ? "&mdash;" : Number(now).toLocaleString()) + "</td>" +
        "<td class=\"col-num col-mono\">" +
          (after === null ? "&mdash;" : Number(after).toLocaleString()) + "</td>" +
        "</tr>";
    });

    if (!body) {
      resetImportPreview();
      showImportStatus((payload.notes || []).join(" ") ||
        "Nothing recognisable was found in that file.", true);
      return;
    }

    el.importPreview.innerHTML = body;
    el.importPreviewWrap.hidden = false;
    state.importValues = values;

    // OVERWRITTEN, NOT COMBINED - and said only when it matters. On a fresh
    // install nobody reads that sentence; on a used one it is the only thing
    // that does. `replaces` is the server's answer to "which of these already
    // has a non-zero value", so the warning appears exactly then.
    var replacing = Object.keys(payload.replaces || {});
    var notes = (payload.notes || []).slice();
    if (replacing.length) {
      notes.unshift("Your current figures will be REPLACED, not added to.");
    }
    el.importWarning.hidden = !notes.length;
    el.importWarning.textContent = notes.join(" ");
    el.importConfirm.hidden = false;
    showImportStatus("");
  }

  function previewImportText(text) {
    if (!String(text || "").trim()) {
      resetImportPreview();
      showImportStatus("That file was empty.", true);
      return;
    }
    importVariables().then(function (names) {
      var kept = keepOnlyCounterLines(text, names);
      if (!kept.trim()) {
        resetImportPreview();
        showImportStatus("No OmenServe counters were found in that file. The " +
          "totals come from the add-ons (mxrarserver, OS-Limits), so an " +
          "install without them has nothing to bring across.", true);
        return;
      }
      return postJson("/api/stats/import/preview", { text: kept })
        .then(function (res) {
          if (!res.ok) {
            resetImportPreview();
            showImportStatus(res.data.error || ("HTTP " + res.status), true);
            return;
          }
          renderImportPreview(res.data);
        });
    }).catch(function (err) {
      resetImportPreview();
      showImportStatus("Could not read that: " + err.message, true);
    });
  }

  el.importFile.addEventListener("change", function () {
    var file = el.importFile.files && el.importFile.files[0];
    if (!file) { return; }
    var reader = new FileReader();
    reader.onload = function () { previewImportText(reader.result); };
    reader.onerror = function () {
      showImportStatus("Could not read that file.", true);
    };
    reader.readAsText(file);
    // Cleared so choosing the SAME file again still fires a change event -
    // otherwise a second attempt after an error looks like a dead button.
    el.importFile.value = "";
  });

  el.importPasteToggle.addEventListener("click", function () {
    var showing = el.importPaste.hidden;
    el.importPaste.hidden = !showing;
    el.importPasteActions.hidden = !showing;
    if (showing) { el.importPaste.focus(); }
  });

  el.importPasteRead.addEventListener("click", function () {
    previewImportText(el.importPaste.value);
  });

  el.importCancel.addEventListener("click", function () {
    resetImportPreview();
    showImportStatus("");
  });

  el.importApply.addEventListener("click", function () {
    if (!state.importValues) { return; }
    el.importApply.disabled = true;
    postJson("/api/stats/import", state.importValues).then(function (res) {
      el.importApply.disabled = false;
      if (!res.ok) {
        showImportStatus(res.data.error || ("HTTP " + res.status), true);
        return;
      }
      resetImportPreview();
      // What ACTUALLY happened, from the server's own before/after, rather
      // than what the page asked for.
      var after = res.data.after || {};
      showImportStatus("Imported. Files sent is now " +
        Number(after.total_files || 0).toLocaleString() + ", and the speed " +
        "record " + Number(after.speed_record || 0).toLocaleString() + " B/s.");
      loadStats();
    }).catch(function (err) {
      el.importApply.disabled = false;
      showImportStatus("Import failed: " + err.message, true);
    });
  });

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
    if (el.updateListBar) { el.updateListBar.style.display = "none"; }
  }

  // "Rebuilding the master list…" for however long a 719k-file library takes,
  // with nothing else on screen, is indistinguishable from a hung process.
  // This says which folder it is in, how far through the folders it is, and
  // how many files it has indexed - the file count moves even while the bar
  // does not, which is what tells an operator it is alive.
  // Seconds as something a person reads. Mirrors commands.describe_duration()
  // deliberately: the same rebuild is reported here and in the debug channel,
  // and two wordings for one number reads as two different measurements.
  function describeDuration(seconds) {
    var total = Math.max(0, Math.floor(Number(seconds) || 0));
    if (total < 60) { return total + "s"; }
    var pad = function (n) { return String(n).padStart(2, "0"); };
    if (total < 3600) {
      return Math.floor(total / 60) + "m " + pad(total % 60) + "s";
    }
    return Math.floor(total / 3600) + "h " + pad(Math.floor((total % 3600) / 60)) + "m";
  }

  function showUpdateListProgress(progress) {
    if (!progress) {
      showUpdateListStatus("Rebuilding the master list…", false);
      return;
    }

    var parts = [];
    if (progress.phase === "writing") {
      parts.push("Writing the list…");
    } else if (progress.folder_count) {
      parts.push("Scanning folder " + progress.folder_index +
                 " of " + progress.folder_count);
      if (progress.folder) { parts.push(progress.folder); }
    } else {
      parts.push("Scanning the library…");
    }
    if (progress.files) {
      parts.push(progress.files.toLocaleString() + " files so far");
    }
    // LAST, because it is the part that changes on every tick. Reading down
    // the line, what the rebuild is DOING should not move about under the eye
    // while the clock counts.
    //
    // Only when the daemon reported one: a progress file written by an older
    // build mid-upgrade has no start time, and "0s" forever would read as a
    // stalled rebuild rather than as a missing field.
    if (progress.elapsed !== null && progress.elapsed !== undefined) {
      parts.push(describeDuration(progress.elapsed));
    }

    el.updateListStatus.textContent = parts.join(" · ");
    el.updateListStatus.classList.remove("is-error");

    if (!el.updateListBar) { return; }
    // Indeterminate while writing: the folder count has nothing left to say
    // once the walk is done, and a bar frozen at its last value would read as
    // a stall during the phase that is genuinely slowest.
    var known = progress.percent !== null && progress.percent !== undefined
                && progress.phase !== "writing";
    el.updateListBar.style.display = "block";
    el.updateListBar.classList.toggle("is-indeterminate", !known);
    el.updateListBarFill.style.width = known ? (progress.percent + "%") : "";
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
        showUpdateListProgress(payload.progress);
        return;
      }
      clearInterval(updateList.pollTimer);
      updateList.pollTimer = null;
      el.updateListRunBtn.disabled = false;
      // #224: "running" alone cannot tell a rebuild that worked from one
      // that failed - this used to say "Done" unconditionally the moment
      // running flipped false, whichever it was.
      // How long it took, on both outcomes. On a failure it is the more
      // useful half of the message: a rebuild that died after four seconds
      // never reached the library, and one that died after forty minutes
      // did - and that is the difference between a typo in a path and a
      // mount that went away mid-walk.
      var took = (payload.seconds === null || payload.seconds === undefined)
        ? "" : " in " + describeDuration(payload.seconds);
      if (payload.ok === false) {
        showUpdateListStatus(
          "Failed" + took + ": " + (payload.error || "unknown error"), true);
      } else {
        showUpdateListStatus(
          "Done" + took + ". Check Stats for the new file count.", false);
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

  // The two ends of the unit display. Everything between them - the baseline,
  // the dirty set, the save body, settings.conf itself - is bytes.
  function bytesToUnit(stored, factor) {
    var n = parseFloat(stored);
    if (!factor || !isFinite(n)) { return stored; }
    // Trailing zeros trimmed: 200 rather than 200.0000, but 0.5 kept. A whole
    // number is the ordinary case and the one worth reading cleanly.
    return String(parseFloat((n / factor).toFixed(4)));
  }

  function unitToBytes(typed, factor) {
    var n = parseFloat(typed);
    // An empty or half-typed box is left alone rather than becoming 0 - the
    // save would otherwise write "no limit" for a field the operator was
    // still in the middle of, and 0 means exactly that for several of these.
    if (!factor || !isFinite(n)) { return typed; }
    return String(Math.round(n * factor));
  }

  // THE CHANNELS THE BOT IS ACTUALLY CONFIGURED TO JOIN, as the operator has
  // them RIGHT NOW - the pending edit if there is one, the saved value
  // otherwise. Same source and same precedence the theme preview reads, for
  // the same reason: a channel typed into Identity & network and not yet
  // saved is still a channel the operator means to be in, and offering the
  // stale list would be offering to bind a list to a channel they have just
  // renamed.
  function configuredChannels() {
    var field = settingsFieldByName("CHANNEL");
    if (!field) { return []; }
    var raw = Object.prototype.hasOwnProperty.call(state.settingsDirty, "CHANNEL")
      ? state.settingsDirty.CHANNEL
      : settingsValueToString(field.value);
    var seen = {};
    var out = [];
    String(raw || "").split(",").forEach(function (part) {
      var name = part.trim();
      if (!name) { return; }
      var key = name.toLowerCase();
      if (seen[key]) { return; }
      seen[key] = true;
      out.push(name);
    });
    return out;
  }

  function settingsFieldByName(name) {
    var found = null;
    (state.settingsCategories || []).forEach(function (category) {
      (category.fields || []).forEach(function (field) {
        if (field.name === name) { found = field; }
      });
    });
    return found;
  }

  // mIRC's sixteen. Every client agrees on these; the 99-colour extension
  // does not, which is why the themes only ever use this range.
  var IRC_COLOURS = [
    "#ffffff", "#000000", "#00007f", "#009300", "#ff0000", "#7f0000",
    "#9c009c", "#fc7f00", "#ffff00", "#00fc00", "#009393", "#00ffff",
    "#0000fc", "#ff00ff", "#7f7f7f", "#d2d2d2"
  ];

  // The same sixteen, named. theme.py's own comments are the vocabulary -
  // "solid green", "royal blue", "light cyan" - so the menu and the presets
  // it is competing with call a colour the same thing.
  var IRC_COLOUR_NAMES = [
    "white", "black", "blue", "green", "red", "maroon", "purple", "orange",
    "yellow", "light green", "cyan", "light cyan", "royal blue", "pink",
    "grey", "light grey"
  ];

  // A ROLE IS A COLOUR CODE, and only some codes are a colour the menus can
  // offer: "\x0304", "\x0304,05", or nothing at all. Returns null for
  // anything else - a code with bold in it, a hand-written oddity, text -
  // and the field then stays the box it has always been.
  //
  // Returning null rather than a best guess is the point. Rewriting a value
  // the operator wrote by hand into the nearest thing a dropdown can say is
  // the one behaviour a picker must never have.
  function parseIrcColour(text) {
    var raw = String(text == null ? "" : text);
    if (!raw) { return { fg: "", bg: "" }; }
    var match = /^\\x03(\d{1,2})(?:,(\d{1,2}))?$/.exec(raw);
    if (!match) { return null; }
    var fg = parseInt(match[1], 10);
    var bg = match[2] === undefined ? null : parseInt(match[2], 10);
    if (fg > 15 || (bg !== null && bg > 15)) { return null; }
    return { fg: String(fg), bg: bg === null ? "" : String(bg) };
  }

  // Two digits, because every preset in theme.py is written that way and one
  // value should have one spelling. mIRC reads "\x034" and "\x0304" alike,
  // but "\x034,05" is ambiguous to some clients where the padded form never
  // is.
  function formatIrcColour(fg, bg) {
    if (fg === "") { return ""; }
    var code = "\\x03" + pad2(fg);
    return bg === "" ? code : code + "," + pad2(bg);
  }

  function pad2(value) {
    var text = String(value);
    return text.length < 2 ? "0" + text : text;
  }

  // WHAT THE CHANNEL WILL SEE, rendered from the same bytes that go on the
  // wire. Handles the three codes the themes use - \x03 colour, \x02 bold,
  // \x0f reset - and passes anything else through as text.
  //
  // A preview that renders differently from a real client is worse than no
  // preview, so this deliberately does the same simple thing every client
  // does: a colour code sets foreground and optional background until the
  // next one, and a reset clears both.
  //
  // Text goes in with escapeHtml() and colours come from the fixed table
  // above - the codes are digits parsed with parseInt, so nothing off the
  // wire reaches an attribute.
  function renderIrcLine(text) {
    var out = "";
    var i = 0;
    var fg = null, bg = null, bold = false;
    var open = false;
    var raw = String(text || "");

    function openSpan() {
      var style = [];
      if (fg !== null && IRC_COLOURS[fg]) { style.push("color:" + IRC_COLOURS[fg]); }
      if (bg !== null && IRC_COLOURS[bg]) {
        style.push("background-color:" + IRC_COLOURS[bg]);
      }
      if (bold) { style.push("font-weight:700"); }
      if (!style.length) { return; }
      out += '<span style="' + style.join(";") + '">';
      open = true;
    }

    function closeSpan() {
      if (open) { out += "</span>"; open = false; }
    }

    function restart() { closeSpan(); openSpan(); }

    while (i < raw.length) {
      var ch = raw.charAt(i);
      if (ch === "\u0003") {
        i++;
        var digits = "";
        while (digits.length < 2 && /[0-9]/.test(raw.charAt(i))) {
          digits += raw.charAt(i); i++;
        }
        if (!digits) {
          // A bare \x03 clears colour and keeps whatever bold is in force.
          fg = null; bg = null; restart(); continue;
        }
        fg = parseInt(digits, 10) % 16;
        if (raw.charAt(i) === ",") {
          var after = "";
          var j = i + 1;
          while (after.length < 2 && /[0-9]/.test(raw.charAt(j))) {
            after += raw.charAt(j); j++;
          }
          // A comma with no digits after it is a literal comma, not an empty
          // background - which is how "\x0304,text" is meant to read.
          if (after) { bg = parseInt(after, 10) % 16; i = j; }
        }
        restart();
        continue;
      }
      if (ch === "\u0002") { bold = !bold; i++; restart(); continue; }
      if (ch === "\u000f") {
        fg = null; bg = null; bold = false; i++; closeSpan(); continue;
      }
      out += escapeHtml(ch);
      i++;
    }
    closeSpan();
    return out;
  }

  // THE THREE ROLES THAT ARE A SURFACE, not a text colour. theme.py defines
  // them that way: border is "the outer block that frames a section",
  // separator "the block between fields", textbox "the plate the text sits
  // on". A block with no background is not a block, and a plate with no
  // background is not a plate - whatever colour the previous segment left
  // behind simply shows through.
  //
  // value, alert and accent are the opposite: they colour figures and
  // secondary text, and a foreground on its own is exactly right for them.
  var IRC_SURFACE_ROLES = ["BORDER", "SEPARATOR", "TEXTBOX"];

  function isSurfaceRole(settingName) {
    return IRC_SURFACE_ROLES.some(function (role) {
      return settingName === "CUSTOM_THEME_" + role;
    });
  }

  // TWO MENUS AND A SWATCH, in place of a box that could not display what it
  // held. See _settings_field() in webserver.py for what the box was showing.
  //
  // The background menu is disabled while the foreground is "theme default",
  // because there is no such code as a background on its own: "\x03,05" is
  // not a colour, and offering it would be offering a value that cannot be
  // saved.
  function ircColourPickerHtml(name, picked) {
    function options(selected, none) {
      var out = '<option value=""' + (selected === "" ? " selected" : "") +
        ">" + escapeHtml(none) + "</option>";
      for (var i = 0; i < IRC_COLOUR_NAMES.length; i++) {
        out += '<option value="' + i + '"' +
          (selected === String(i) ? " selected" : "") + ">" +
          escapeHtml(pad2(i) + " " + IRC_COLOUR_NAMES[i]) + "</option>";
      }
      return out;
    }

    return '<span class="irc-colour" data-irc-colour="' + escapeHtml(name) + '">' +
      '<span class="irc-colour-swatch" data-irc-swatch></span>' +
      '<select data-irc-part="fg" aria-label="Foreground colour">' +
        options(picked.fg, "Theme default") +
      "</select>" +
      '<select data-irc-part="bg" aria-label="Background colour"' +
        (picked.fg === "" ? " disabled" : "") + ">" +
        // NOT "No background", which is what this said and is not what it
        // does. A colour code carrying only a foreground leaves the
        // background exactly as the previous segment set it - that is the IRC
        // formatting spec, not an implementation quirk - so the honest label
        // is what actually happens. Reported from the beta: a border of blue
        // on red with the text box left "No background" painted every field
        // after it red, and the preview was right.
        options(picked.bg, "Keep previous") +
      "</select>" +
      surfaceWarningHtml(name, picked) +
    "</span>";
  }

  // Only on the three that are a surface, and only when there is something to
  // warn about. A note that is always there is a note nobody reads.
  function surfaceWarningHtml(settingName, picked) {
    if (!isSurfaceRole(settingName)) { return ""; }
    if (picked.fg === "" || picked.bg !== "") { return ""; }
    return '<span class="irc-colour-warning">' +
      "keeps the previous background \u2014 this one is drawn as a block" +
      "</span>";
  }

  // The swatch is what the code MEANS, shown next to the menus that made it:
  // a solid block is a foreground on a background, which is exactly how the
  // border and separator roles are built (see theme.py - fg == bg fills the
  // cell). Without it, "04 red on 05 maroon" is two words rather than a
  // colour.
  function paintIrcSwatch(holder) {
    var swatch = holder.querySelector("[data-irc-swatch]");
    var fg = holder.querySelector('[data-irc-part="fg"]').value;
    var bg = holder.querySelector('[data-irc-part="bg"]').value;
    if (!swatch) { return; }
    if (fg === "") {
      swatch.removeAttribute("style");
      swatch.classList.add("is-unset");
      return;
    }
    swatch.classList.remove("is-unset");
    swatch.style.color = IRC_COLOURS[Number(fg)];
    swatch.style.backgroundColor = bg === ""
      ? "transparent" : IRC_COLOURS[Number(bg)];
  }

  function attachIrcColourPickers() {
    el.settingsFields.querySelectorAll("[data-irc-colour]").forEach(function (holder) {
      var name = holder.dataset.ircColour;
      var fg = holder.querySelector('[data-irc-part="fg"]');
      var bg = holder.querySelector('[data-irc-part="bg"]');
      paintIrcSwatch(holder);

      function changed() {
        // A background with no foreground is not a code. Clearing it here
        // rather than refusing the save means the operator never assembles a
        // value that cannot exist.
        bg.disabled = fg.value === "";
        if (bg.disabled) { bg.value = ""; }
        paintIrcSwatch(holder);
        recordSettingChange(name, formatIrcColour(fg.value, bg.value), holder);
      }

      fg.addEventListener("change", changed);
      bg.addEventListener("change", changed);
    });
  }

  // WHICH CATEGORY EACH EXTRA EDITOR BELONGS TO, named rather than typed out
  // at each of its call sites.
  //
  // Three editors are not settings and cannot be: Served folders and Serve
  // more than one list are ordered lists of {label, path} validated as a SET,
  // and On connect is a list of commands. They live in their own JSON files
  // behind their own endpoints, and they are stitched into a settings
  // category so an operator finds them where they would look.
  //
  // They were stitched onto a category id that stopped existing when
  // the Settings page was regrouped, which took all three off the dashboard
  // with nothing failing anywhere. Reported from the beta: "what happened with
  // multi folder / channel? cant see it in settings at all."
  //
  // A constant does not by itself stop that happening again, so
  // test_every_settings_category_the_page_asks_for_exists.py checks each of
  // these against what the server actually sends.
  var SERVED_FOLDERS_CATEGORY = "your-list";   // where Music directory lives
  var ON_CONNECT_CATEGORY = "identity";        // registration, before the JOIN

  function settingsFieldHtml(field) {
    var isDirty = Object.prototype.hasOwnProperty.call(state.settingsDirty, field.name);
    var nameClass = "settings-field-name" + (isDirty ? " is-dirty" : "");
    var control;

    if (field.irc_colour) {
      var current = isDirty ? state.settingsDirty[field.name]
                            : settingsValueToString(field.value);
      var picked = parseIrcColour(current);
      if (picked) {
        control = ircColourPickerHtml(field.name, picked);
      } else {
        // A value the menus cannot say. It stays a text box, and says why -
        // silently replacing it with the nearest colour a dropdown can offer
        // would be losing the operator's work to make the page tidier.
        control = '<input type="text" autocomplete="off" data-setting="' +
          escapeHtml(field.name) + '">' +
          '<span class="settings-field-note">Set by hand to something the ' +
          "menus cannot offer, so it is left as text.</span>";
      }
    } else if (field.choices) {

      // A fixed few, not free text. The three list formats are the first: a
      // typed "ZIP" or "tar" would be refused by the save with a reason, but
      // being refused is a worse way to find out than never being offered it.
      // THE STORED VALUE HAS TO BE THE SELECTED ONE, and it was not: no
      // option carried `selected` and nothing assigned select.value after
      // this markup was inserted, so every dropdown on this page rendered
      // showing its FIRST choice whatever the daemon was actually using.
      //
      // From the beta: "i set packet size to 64kb and when i press save and
      // rehash i see it back to 4kb". The save had worked - 4096 is simply
      // the first DCC_BLOCK_SIZE choice, and the page could not show
      // anything else. All four selects were affected (LIST_FORMAT always
      // read "txt", THEME "classic", ADMIN_CHAT_MODE "auto"), which is the
      // worse half: the page states a value the daemon is not using, and an
      // operator who reads it as correct and leaves it alone is agreeing to
      // something they were never shown.
      //
      // Compared as STRINGS. field.value arrives as JSON, so it is an int
      // for DCC_BLOCK_SIZE and a string for LIST_FORMAT, while an <option>
      // value is always text - a strict === between the two is false for
      // every numeric choice, which is exactly how this survived.
      //
      // A pending edit wins over the stored value, like the checkbox branch
      // below: a re-render while the save bar is dirty must not silently
      // discard what the operator picked.
      var current = isDirty ? state.settingsDirty[field.name]
                            : settingsValueToString(field.value);
      var options = field.choices.map(function (choice, i) {
        // The stored value stays the value; only what the operator reads
        // changes. DCC_BLOCK_SIZE is the first: "64 KB" rather than "65536".
        var text = (field.choice_labels && field.choice_labels[i]) || choice;
        return '<option value="' + escapeHtml(choice) + '"' +
          (String(choice) === current ? " selected" : "") +
          ">" + escapeHtml(text) + "</option>";
      }).join("");
      control = '<select data-setting="' + escapeHtml(field.name) + '">' + options + "</select>";
    } else if (field.type === "bool") {
      var checked = isDirty ? state.settingsDirty[field.name] === "true" : !!field.value;
      control = '<input type="checkbox" data-setting="' + escapeHtml(field.name) + '"' +
        (checked ? " checked" : "") + ">";
    } else if (field.type === "int" || field.type === "float") {
      // A size gets a unit chip and is typed in that unit. The FILE still
      // holds bytes - see SETTINGS_UNITS in webserver.py - so nothing
      // migrates and a hand-edited settings.conf is unchanged. `step="any"`
      // because a byte count need not land on a whole unit, and refusing to
      // display a value the daemon is already using would be worse than
      // showing a fraction of one.
      var unitChip = field.unit
        ? '<span class="settings-field-unit">' + escapeHtml(field.unit) + "</span>"
        : "";
      control = '<input type="number" step="' +
        (field.unit || field.type === "float" ? "any" : "1") +
        '" data-setting="' + escapeHtml(field.name) + '">' + unitChip;
    } else {
      control = '<input type="text" autocomplete="off" data-setting="' +
        escapeHtml(field.name) + '">';
    }

    // A setting whose unset value is not the same as false says so. Only
    // WEBUI_CONSOLE_ENABLED carries one today: it is declared `bool = None`
    // and None means "on while the dashboard is loopback-only", so the
    // checkbox alone was telling a stock install that the remote admin
    // console was off while it was on.
    //
    // escapeHtml() into TEXT content, which is what it encodes correctly. It
    // does not encode quotes, so this must never become an attribute.
    var note = field.note
      ? '<span class="settings-field-note">' + escapeHtml(field.note) + "</span>"
      : "";

    return '<div class="settings-field-row">' +
      '<span class="' + nameClass + '">' + escapeHtml(field.label || field.name) +
      note + '</span>' +
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

    // Typed in MB or KB, stored in bytes. Converting HERE rather than at save
    // time keeps state.settingsDirty in the same unit as the baseline it is
    // compared against and the payload it is posted as - so the dirty marker,
    // the save bar and the request body all keep working unchanged.
    var field = settingsFieldByName(name);
    if (field && field.unit && input.type !== "checkbox") {
        newValue = unitToBytes(newValue, field.unit_factor);
    }

    recordSettingChange(name, newValue, input);
  }

  // WHAT AN EDIT IS, for a control of any shape. The colour picker is two
  // selects that together mean one setting, so it cannot go through the
  // handler above - and everything after the value is computed is identical
  // for both, including the part that is easy to get subtly wrong: a value
  // equal to the baseline is not an edit, and must be REMOVED from the dirty
  // set rather than stored, or the save bar counts a field the operator has
  // put back exactly as they found it.
  function recordSettingChange(name, newValue, sourceEl) {
    var baselineStr = settingsValueToString(state.settingsBaseline[name]);

    if (newValue === baselineStr) {
      delete state.settingsDirty[name];
    } else {
      state.settingsDirty[name] = newValue;
    }

    var row = sourceEl.closest(".settings-field-row");
    var label = row && row.querySelector(".settings-field-name");
    if (label) {
      label.classList.toggle("is-dirty", Object.prototype.hasOwnProperty.call(state.settingsDirty, name));
    }
    updateSettingsSaveBar();
  }

  function updateSettingsSaveBar() {
    // Colours are the one setting whose effect is not described by its own
    // value, so the sample follows every keystroke rather than waiting for a
    // save that would publish it to a channel first.
    if (state.settingsActiveCategory === "appearance") {
      refreshThemePreview();
    }
    var count = Object.keys(state.settingsDirty).length;
    el.settingsSaveBtn.disabled = count === 0;
    el.settingsSavebarText.classList.toggle("is-dirty", count > 0);
    el.settingsSavebarText.textContent = count === 0
      ? "All changes saved"
      : (count + (count === 1 ? " unsaved change" : " unsaved changes"));
  }

  // The served-folder editor. Markup only - NO VALUES - for the reason the
  // whole of this file repeats: escapeHtml() is textContent -> innerHTML,
  // which leaves a double quote alone, so a path containing one would close
  // value="…" and everything after it would be parsed as markup. Every value
  // here is assigned as a .value PROPERTY by attachFolderRows() below.
  // MORE THAN ONE LIST (#26). This replaces the single folders editor rather
  // than sitting beside it: two places to edit folders, one of which quietly
  // does nothing, is worse than either on its own. With one list the familiar
  // editor stays exactly where it was; the button below is what moves it.
  // Commands sent once the server has registered us and BEFORE we join.
  // The ordering is the whole point - see on_connect.py.
  function onConnectSectionHtml() {
    var data = state.onConnect || { commands: [], delay_seconds: 2 };

    var note = "";
    if (state.onConnectNote) {
      note = '<p class="served-folder-note ' +
        (state.onConnectNote.ok ? "is-ok" : "is-error") + '">' +
        escapeHtml(state.onConnectNote.text) +
        (state.onConnectNote.problems || []).map(function (line) {
          return "<br>\u2022 " + escapeHtml(line);
        }).join("") + "</p>";
    }

    // NO VALUE IN AN ATTRIBUTE. These lines hold an X password and
    // escapeHtml() leaves a double quote alone - the textarea's contents are
    // assigned as a property in attachOnConnectRows(), like every other value
    // on this page.
    return '<div class="served-folders">' +
      "<h3>On connect</h3>" +
      '<p class="served-folder-summary">' +
        "Sent once the server has registered you and <strong>before</strong> " +
        "joining - one command per line, exactly as you would type it into a " +
        "client. On Undernet that ordering matters: logging in to X takes " +
        "<code>+x</code>, and joining first shows your real host to everyone " +
        "already in the channel. Use <code>%nick%</code> for the nickname the " +
        "server actually gave you." +
      "</p>" +
      '<textarea class="on-connect-commands" rows="5" spellcheck="false" ' +
        'placeholder="PRIVMSG X@channels.undernet.org :LOGIN yourname yourpass' +
        '&#10;MODE %nick% +x" aria-label="Commands to send on connect"></textarea>' +
      '<div class="on-connect-delay">' +
        '<label for="on-connect-delay-input">Seconds between commands</label>' +
        '<input type="number" id="on-connect-delay-input" ' +
          'class="on-connect-delay-input" min="0" max="' +
          escapeHtml(String(data.max_delay_seconds || 60)) + '" step="1">' +
      "</div>" +
      '<div class="served-folder-actions">' +
        '<button type="button" class="btn btn-accent on-connect-save">' +
        "Save on-connect commands</button>" +
      "</div>" + note + "</div>";
  }

  function attachOnConnectRows() {
    var data = state.onConnect || { commands: [], delay_seconds: 2 };
    var box = el.settingsFields.querySelector(".on-connect-commands");
    var delay = el.settingsFields.querySelector(".on-connect-delay-input");
    if (!box || !delay) { return; }

    box.value = (data.commands || []).join("\n");
    delay.value = data.delay_seconds === undefined ? 2 : data.delay_seconds;
  }

  function loadOnConnect() {
    return fetchJson("/api/on-connect")
      .then(function (payload) {
        state.onConnect = payload;
        if (state.active === "settings"
            && state.settingsActiveCategory === ON_CONNECT_CATEGORY) {
          renderSettingsCategory();
        }
      })
      .catch(function () { /* the settings page still works without it */ });
  }

  function saveOnConnect() {
    var box = el.settingsFields.querySelector(".on-connect-commands");
    var delay = el.settingsFields.querySelector(".on-connect-delay-input");
    if (!box || !delay) { return; }

    return postJson("/api/on-connect", {
      commands: box.value,
      delay_seconds: Number(delay.value)
    }).then(function (res) {
      if (res.ok) {
        state.onConnect = {
          commands: res.data.commands || [],
          delay_seconds: res.data.delay_seconds,
          max_delay_seconds: (state.onConnect || {}).max_delay_seconds
        };
        state.onConnectNote = { ok: true, text: res.data.message || "Saved." };
      } else {
        // Every fault at once, newline separated, the same way the folder and
        // list endpoints answer.
        var message = (res.data && res.data.error) || "Could not save.";
        var parts = message.split("\n");
        state.onConnectNote = {
          ok: false,
          text: parts.length > 1 ? "Could not save:" : message,
          problems: parts.length > 1 ? parts : []
        };
      }
      renderSettingsCategory();
    });
  }

  function listsSectionHtml() {
    var draft = state.listsDraft || [];

    var blocks = draft.map(function (entry, index) {
      var folderRows = (entry.folders || []).map(function (_f, fIndex) {
        return '<div class="list-folder-row" data-list-index="' + index +
          '" data-folder-index="' + fIndex + '">' +
          '<input type="text" class="list-folder-name" placeholder="Label" aria-label="Folder label">' +
          '<input type="text" class="list-folder-path" placeholder="Full path to the folder" aria-label="Folder path">' +
          '<button type="button" class="served-folder-btn list-folder-remove" title="Remove folder" aria-label="Remove folder">\u00d7</button>' +
          "</div>";
      }).join("");

      return '<div class="served-list-block" data-list-index="' + index + '">' +
        '<div class="served-list-head">' +
          '<input type="text" class="served-list-name" placeholder="List name" aria-label="List name">' +
          '<label class="served-list-primary-label">' +
            '<input type="radio" name="served-list-primary" class="served-list-primary">' +
            " Primary</label>" +
          '<button type="button" class="served-folder-btn served-list-remove" title="Remove list" aria-label="Remove list">\u00d7</button>' +
        "</div>" +
        servedListChannelsHtml(entry, index) +
        '<div class="served-list-folders">' + folderRows + "</div>" +
        '<button type="button" class="btn btn-small list-folder-add">Add folder</button>' +
        "</div>";
    }).join("");

    var note = "";
    if (state.listsNote) {
      note = '<p class="served-folder-note ' + (state.listsNote.ok ? "is-ok" : "is-error") + '">' +
        escapeHtml(state.listsNote.text) +
        (state.listsNote.problems || []).map(function (line) {
          return "<br>\u2022 " + escapeHtml(line);
        }).join("") + "</p>";
    }

    return '<div class="served-folders">' +
      "<h3>Served lists</h3>" +
      '<p class="served-folder-summary">' +
        "Each list is built from its own folders and answered in its own " +
        "channels. A channel named by no list is not served at all; a list " +
        "naming no channels answers everywhere, which only makes sense for " +
        "one of them. The primary is what a private message means." +
      "</p>" +
      blocks +
      '<div class="served-folder-actions">' +
        '<button type="button" class="btn served-list-add">Add list</button>' +
        '<button type="button" class="btn btn-accent served-list-save">Save lists</button>' +
      "</div>" + note + "</div>";
  }

  // PICK FROM THE CHANNELS ALREADY CONFIGURED, rather than typing them a
  // second time.
  //
  // WHY THIS IS NOT TIDYING. library.list_for_request() matches exactly, and
  // its rule 3 is that once the primary binds any channels at all, a channel
  // with nothing bound to it gets NOTHING - no advert, no requests answered.
  // So a single mistyped character in this field does not bind one channel
  // wrongly; it silences the bot in the real channel, with no error anywhere
  // and nothing on the page saying why. Verified, not assumed:
  // tests/test_a_list_binds_the_channels_you_configured.py starts by
  // reproducing it.
  //
  // A CHANNEL BOUND HERE THAT IS NOT IN THE JOIN LIST IS STILL SHOWN, ticked,
  // and marked. It is the operator's own data and dropping it silently would
  // be the picker deciding what they meant - and more to the point, an
  // install already in this state is one where the row IS the diagnosis. It
  // is the only place the mistake is visible.
  //
  // Nothing ticked still means everywhere, which is what an empty field has
  // always meant and what every install today has.
  function servedListChannelsHtml(entry, index) {
    var bound = (entry.channels || []).map(function (name) {
      return String(name).trim();
    }).filter(function (name) { return name.length > 0; });
    var boundKeys = {};
    bound.forEach(function (name) { boundKeys[name.toLowerCase()] = true; });

    var offered = configuredChannels();
    var offeredKeys = {};
    offered.forEach(function (name) { offeredKeys[name.toLowerCase()] = true; });

    var extra = bound.filter(function (name) {
      return !offeredKeys[name.toLowerCase()];
    });

    if (!offered.length && !extra.length) {
      return '<p class="served-list-channels-empty">' +
        "No channels are configured yet. Add them under Identity &amp; " +
        "network, then come back to bind this list to one. Until then this " +
        "list serves every channel." +
        "</p>";
    }

    var boxes = offered.map(function (name) {
      return channelBoxHtml(index, name, !!boundKeys[name.toLowerCase()], false);
    }).concat(extra.map(function (name) {
      return channelBoxHtml(index, name, true, true);
    })).join("");

    var warning = extra.length
      ? '<p class="served-list-channels-warning">' +
        (extra.length === 1
          ? "One channel bound here is not in your join list, so nothing "
          : extra.length + " channels bound here are not in your join list, so nothing ") +
        "arrives from it. Untick it, or add it under Identity &amp; network." +
        "</p>"
      : "";

    return '<div class="served-list-channels" data-list-index="' + index + '">' +
      '<p class="served-list-channels-label">Serves' +
        (bound.length ? "" : " every channel") + "</p>" +
      '<div class="served-list-channel-boxes">' + boxes + "</div>" +
      warning +
      "</div>";
  }

  // The NAME goes on via .dataset in attachListRows(), never concatenated
  // into the markup: a channel name is operator input and escapeHtml() does
  // not encode a double quote, which is the same rule every other row in this
  // file follows.
  function channelBoxHtml(index, name, checked, unconfigured) {
    return '<label class="served-list-channel' +
      (unconfigured ? " is-unconfigured" : "") + '">' +
      '<input type="checkbox" class="served-list-channel-box"' +
      ' data-list-index="' + index + '"' + (checked ? " checked" : "") + ">" +
      "<span></span></label>";
  }

  // Values as PROPERTIES, never concatenated into value="…": escapeHtml() is
  // textContent -> innerHTML and leaves a double quote alone, so a path
  // containing one would close the attribute. Same rule the folder rows
  // follow. Typing does not re-render, so the caret stays where it is.
  // The same order servedListChannelsHtml() drew them in - configured first,
  // then anything bound that is not configured. Derived rather than stored,
  // so the boxes and their names cannot drift apart.
  function channelNamesFor(entry, index) {
    var bound = (entry.channels || []).map(function (name) {
      return String(name).trim();
    }).filter(function (name) { return name.length > 0; });
    var offered = configuredChannels();
    var offeredKeys = {};
    offered.forEach(function (name) { offeredKeys[name.toLowerCase()] = true; });
    return offered.concat(bound.filter(function (name) {
      return !offeredKeys[name.toLowerCase()];
    }));
  }

  // What is ticked, in the order it is drawn. Read off the DOM rather than
  // accumulated as they are clicked: the draft is what gets sent, and one
  // read of the boxes cannot disagree with them.
  function tickedChannels(block) {
    var out = [];
    block.querySelectorAll(".served-list-channel-box").forEach(function (box) {
      if (box.checked && box.dataset.channel) { out.push(box.dataset.channel); }
    });
    return out;
  }

  function attachListRows() {
    var draft = state.listsDraft || [];

    el.settingsFields.querySelectorAll(".served-list-block").forEach(function (block) {
      var index = parseInt(block.dataset.listIndex, 10);
      var entry = draft[index];
      if (!entry) { return; }

      var nameInput = block.querySelector(".served-list-name");
      var primaryInput = block.querySelector(".served-list-primary");

      nameInput.value = entry.name || "";
      primaryInput.checked = !!entry.primary;

      nameInput.addEventListener("input", function () {
        draft[index].name = nameInput.value;
      });

      // The channel NAME is assigned as a property, never written into the
      // markup - see channelBoxHtml(). The label text goes in as textContent
      // for the same reason.
      block.querySelectorAll(".served-list-channel").forEach(function (label, position) {
        var box = label.querySelector(".served-list-channel-box");
        var name = channelNamesFor(entry, index)[position];
        if (name === undefined) { return; }
        box.dataset.channel = name;
        label.querySelector("span").textContent = name;
        box.addEventListener("change", function () {
          draft[index].channels = tickedChannels(block);
        });
      });
      primaryInput.addEventListener("change", function () {
        // Exactly one, enforced here as well as on the server: a radio group
        // already allows only one, but the draft is what gets sent.
        draft.forEach(function (other, otherIndex) {
          other.primary = otherIndex === index;
        });
      });
    });

    el.settingsFields.querySelectorAll(".list-folder-row").forEach(function (row) {
      var listIndex = parseInt(row.dataset.listIndex, 10);
      var folderIndex = parseInt(row.dataset.folderIndex, 10);
      var entry = draft[listIndex];
      if (!entry || !entry.folders || !entry.folders[folderIndex]) { return; }
      var folder = entry.folders[folderIndex];

      var nameInput = row.querySelector(".list-folder-name");
      var pathInput = row.querySelector(".list-folder-path");
      nameInput.value = folder.name || "";
      pathInput.value = folder.path || "";
      nameInput.addEventListener("input", function () {
        folder.name = nameInput.value;
      });
      pathInput.addEventListener("input", function () {
        folder.path = pathInput.value;
      });
    });
  }

  function handleListButton(button) {
    var draft = state.listsDraft || (state.listsDraft = []);

    if (button.classList.contains("served-list-start")) {
      // Seeded from what is already served, so "serve more than one list"
      // does not begin by throwing away the folders already configured. It
      // is not saved until the operator saves it.
      state.listsDraft = [{
        name: "Main",
        primary: true,
        channels: [],
        folders: (state.foldersDraft || state.folders || []).map(function (f) {
          return { name: f.name, path: f.path };
        })
      }];
      state.listsSource = "file";
      state.listsNote = {
        ok: true,
        text: "Add a second list, name the channels each one serves, then " +
              "save. Nothing changes until you do."
      };
      renderSettingsCategory();
      return;
    }

    if (button.classList.contains("served-list-save")) {
      saveLists();
      return;
    }

    if (button.classList.contains("served-list-add")) {
      draft.push({ name: "", primary: draft.length === 0, channels: [], folders: [] });
    } else if (button.classList.contains("served-list-remove")) {
      var block = button.closest(".served-list-block");
      var removeIndex = parseInt(block.dataset.listIndex, 10);
      var wasPrimary = draft[removeIndex] && draft[removeIndex].primary;
      draft.splice(removeIndex, 1);
      // Removing the primary must leave one behind, or the save is refused
      // for a reason the operator did not choose.
      if (wasPrimary && draft.length) { draft[0].primary = true; }
    } else if (button.classList.contains("list-folder-add")) {
      var addBlock = button.closest(".served-list-block");
      var addIndex = parseInt(addBlock.dataset.listIndex, 10);
      if (draft[addIndex]) {
        draft[addIndex].folders = draft[addIndex].folders || [];
        draft[addIndex].folders.push({ name: "", path: "" });
      }
    } else if (button.classList.contains("list-folder-remove")) {
      var row = button.closest(".list-folder-row");
      var listIndex = parseInt(row.dataset.listIndex, 10);
      var folderIndex = parseInt(row.dataset.folderIndex, 10);
      if (draft[listIndex] && draft[listIndex].folders) {
        draft[listIndex].folders.splice(folderIndex, 1);
      }
    }
    state.listsNote = null;
    renderSettingsCategory();
  }

  function loadLists() {
    return fetchJson("/api/lists")
      .then(function (payload) {
        state.lists = payload.lists || [];
        state.listsSource = payload.source || "";
        // Only seeded from the server when there is no edit in progress -
        // a poll landing mid-edit must not throw away what was typed.
        if (state.listsDraft === null) {
          state.listsDraft = state.lists.map(function (entry) {
            return {
              name: entry.name,
              primary: !!entry.primary,
              channels: (entry.channels || []).slice(),
              folders: (entry.folders || []).map(function (f) {
                return { name: f.name, path: f.path };
              })
            };
          });
        }
        if (state.active === "settings"
            && state.settingsActiveCategory === SERVED_FOLDERS_CATEGORY) {
          renderSettingsCategory();
        }
      })
      .catch(function () { /* the settings page still works without it */ });
  }

  function saveLists() {
    var draft = state.listsDraft || [];
    return postJson("/api/lists", { lists: draft })
      .then(function (res) {
        if (res.ok) {
          state.listsNote = {
            ok: true,
            text: res.data.message || "Saved."
          };
          // Dropped so the reload seeds it from what the server actually
          // wrote, rather than from what was typed - a name the server
          // trimmed would otherwise stay untrimmed on screen.
          state.listsDraft = null;
          state.lists = res.data.lists || [];
          return loadLists();
        }
        // The server returns every fault at once, newline separated, so an
        // operator fixing three things is told about three things.
        var message = (res.data && res.data.error) || "Could not save.";
        var parts = message.split("\n");
        state.listsNote = {
          ok: false,
          text: parts.length > 1 ? "Could not save:" : message,
          problems: parts.length > 1 ? parts : []
        };
        renderSettingsCategory();
      });
  }

  function foldersSectionHtml() {
    var draft = state.foldersDraft || [];
    var rows = draft.map(function (_row, index) {
      return '<div class="served-folder-row" data-served-folder-index="' + index + '">' +
        '<input type="text" class="served-folder-name" placeholder="Label" aria-label="Folder label">' +
        '<input type="text" class="served-folder-path" placeholder="Full path to the folder" aria-label="Folder path">' +
        (state.foldersBrowserEnabled
          ? '<button type="button" class="served-folder-btn served-folder-browse" title="Browse for a folder">Browse</button>'
          : "") +
        '<button type="button" class="served-folder-btn served-folder-up" title="Move up" aria-label="Move up">\u2191</button>' +
        '<button type="button" class="served-folder-btn served-folder-down" title="Move down" aria-label="Move down">\u2193</button>' +
        '<button type="button" class="served-folder-btn served-folder-remove" title="Remove" aria-label="Remove">\u00d7</button>' +
        "</div>";
    }).join("");

    var note = "";
    if (state.foldersNote) {
      note = '<p class="served-folder-note ' + (state.foldersNote.ok ? "is-ok" : "is-error") + '">' +
        escapeHtml(state.foldersNote.text) +
        (state.foldersNote.problems || []).map(function (line) {
          return "<br>\u2022 " + escapeHtml(line);
        }).join("") + "</p>";
    }

    var summary;
    if (state.foldersSource === "file") {
      summary = "Serving " + draft.length + " folder" + (draft.length === 1 ? "" : "s") +
        ", in this order. The label is what users see as the first part of every path.";
    } else if (state.foldersSource === "file_directory") {
      summary = "No folder list yet \u2014 serving the single Music directory below. " +
        "Add a folder here to serve more than one.";
    } else {
      summary = "Nothing is being served yet. Add a folder here, or set Music directory below.";
    }

    // NO PATH IN ANY ATTRIBUTE. An entry is addressed by its INDEX into
    // state.browse.entries, and the handler looks the path up from there.
    // escapeHtml() is textContent -> innerHTML and leaves a double quote
    // alone, and a directory name on Linux may contain one - so a path
    // concatenated into data-path="…" would break out of the attribute. Same
    // rule the folder rows and the file lists already follow.
    var browsePanel = "";
    if (state.browse) {
      var b = state.browse;
      var list;
      if (b.error) {
        list = '<p class="served-folder-note is-error">' + escapeHtml(b.error) + "</p>";
      } else if (!b.entries.length) {
        list = '<p class="browse-empty">No folders in here.</p>';
      } else {
        list = '<ul class="browse-list">' + b.entries.map(function (entry, index) {
          return '<li><button type="button" class="browse-entry" data-browse-index="' +
            index + '">' + escapeHtml(entry.name) + "</button></li>";
        }).join("") + "</ul>";
      }

      browsePanel = '<div class="browse-panel">' +
        '<div class="browse-head">' +
          '<span class="browse-where">' +
            escapeHtml(b.at_root ? "This machine" : b.path) + "</span>" +
          '<button type="button" class="btn btn-small browse-close">Close</button>' +
        "</div>" +
        (b.at_root ? "" :
          '<button type="button" class="btn btn-small browse-up">\u2191 Up</button>') +
        list +
        (b.truncated
          ? '<p class="browse-empty">Only the first ' + b.entries.length +
            " folders are shown.</p>"
          : "") +
        (b.at_root ? "" :
          '<div class="browse-actions">' +
            '<button type="button" class="btn btn-accent browse-use">Use this folder</button>' +
          "</div>") +
        "</div>";
    }

    var offNote = "";
    if (!state.foldersBrowserEnabled) {
      offNote = '<p class="served-folder-summary">Type the full path to each folder. ' +
        "A folder picker is available if you turn on \u201cFolder picker on the " +
        "Settings page\u201d under Web dashboard.</p>";
    }

    return '<div class="served-folder-section">' +
      '<h2 class="settings-category-title">Served folders</h2>' +
      '<p class="served-folder-summary">' + escapeHtml(summary) + "</p>" +
      offNote +
      '<div class="served-folder-rows">' + rows + "</div>" +
      browsePanel +
      '<div class="served-folder-actions">' +
        '<button type="button" class="btn served-folder-add">Add folder</button>' +
        '<button type="button" class="btn btn-accent served-folder-save">Save folders</button>' +
      "</div>" + note + "</div>";
  }

  // Assigns every row's values as properties and keeps the draft in step with
  // typing. Typing deliberately does NOT re-render: rebuilding the rows on
  // each keystroke would take the caret with it.
  function attachFolderRows() {
    var draft = state.foldersDraft || [];
    el.settingsFields.querySelectorAll(".served-folder-row").forEach(function (row) {
      var index = parseInt(row.dataset.servedFolderIndex, 10);
      var entry = draft[index] || { name: "", path: "" };
      var nameInput = row.querySelector(".served-folder-name");
      var pathInput = row.querySelector(".served-folder-path");
      nameInput.value = entry.name || "";
      pathInput.value = entry.path || "";
      nameInput.addEventListener("input", function () {
        draft[index].name = nameInput.value;
      });
      pathInput.addEventListener("input", function () {
        draft[index].path = pathInput.value;
      });
    });
  }

  function loadFolders() {
    return fetchJson("/api/folders")
      .then(function (payload) {
        state.folders = payload.folders || [];
        state.foldersSource = payload.source || "";
        state.foldersBrowserEnabled = !!payload.browser_enabled;
        // Only seeded from the server when there is no edit in progress.
        if (state.foldersDraft === null) {
          state.foldersDraft = state.folders.map(function (f) {
            return { name: f.name, path: f.path };
          });
        }
        if (state.active === "settings"
            && state.settingsActiveCategory === SERVED_FOLDERS_CATEGORY) {
          renderSettingsCategory();
        }
      })
      .catch(function () { /* the settings page still works without it */ });
  }

  // Addressed by the ROW OBJECT, not its index.
  //
  // The picker is a panel that stays open while the rows behind it can still
  // be added to, removed and reordered - every one of those renumbers the
  // draft. An index captured when the panel opened therefore points at
  // whichever row happens to sit there by the time "Use this folder" is
  // pressed, and the chosen path lands in the wrong one. Found by audit.
  //
  // The draft entries are objects, so holding the reference survives any
  // amount of reordering. The indexOf() check before writing covers the one
  // case a reference cannot survive: the row having been removed.
  function openBrowse(row, path) {
    fetchJsonAllowingError("/api/folders/browse?path=" + encodeURIComponent(path || ""))
      .then(function (res) {
        var payload = res.data || {};
        state.browse = {
          row: row,
          path: payload.path || "",
          parent: payload.parent,
          at_root: !!payload.at_root,
          entries: payload.entries || [],
          truncated: !!payload.truncated,
          error: payload.error || null
        };
        renderSettingsCategory();
      })
      .catch(function (err) {
        state.browse = { row: row, path: path || "", parent: "",
                         at_root: false, entries: [], truncated: false,
                         error: err.message };
        renderSettingsCategory();
      });
  }

  function saveFolders() {
    var rows = (state.foldersDraft || []).filter(function (entry) {
      return String(entry.path || "").trim() !== "";
    });
    postJson("/api/folders", { folders: rows }).then(function (res) {
      if (res.ok) {
        state.folders = res.data.folders || [];
        state.foldersSource = res.data.source || "";
        state.foldersDraft = state.folders.map(function (f) {
          return { name: f.name, path: f.path };
        });
        state.foldersNote = {
          ok: true,
          text: res.data.written
            ? "Saved " + res.data.written + " folder" +
              (res.data.written === 1 ? "" : "s") +
              ". Rebuild the list from Tools before they appear in it."
            : "Folder list cleared \u2014 back to the single Music directory."
        };
      } else {
        state.foldersNote = {
          ok: false,
          text: (res.data && res.data.error) || "Could not save the folders.",
          problems: (res.data && res.data.problems) || []
        };
      }
      renderSettingsCategory();
    });
  }

  function themePreviewHtml() {
    return (
      '<div class="theme-preview">' +
        '<p class="theme-preview-label">What the channel sees</p>' +
        // Classes, not ids. This panel lives inside a container that is
        // rebuilt whenever the category changes, so an id would be a global
        // name for something that comes and goes - and the page's own guard
        // refuses a lookup by an id the markup does not contain.
        '<div class="theme-preview-line theme-preview-advert">' +
          "Loading&hellip;</div>" +
        '<div class="theme-preview-line theme-preview-notice"></div>' +
        '<p class="theme-preview-note">The periodic advert, and the notice ' +
          "posted when a send finishes. Between them they use all six " +
          "colours - the advert never uses the accent, so both are shown." +
        "</p>" +
      "</div>");
  }

  // WHAT IS ON SCREEN, not what is saved. The point of a preview is the
  // colour you have just typed and not committed, so the pending edits go
  // with the request - the server renders them without touching its own
  // config, which is being read by a daemon serving a channel.
  function refreshThemePreview() {
    var advert = el.settingsFields.querySelector(".theme-preview-advert");
    var notice = el.settingsFields.querySelector(".theme-preview-notice");
    if (!advert || !notice) { return; }

    var wanted = {};
    (state.settingsCategories || []).forEach(function (category) {
      category.fields.forEach(function (field) {
        if (field.name !== "THEME" && field.name.indexOf("CUSTOM_THEME_") !== 0) {
          return;
        }
        wanted[field.name] = Object.prototype.hasOwnProperty.call(
          state.settingsDirty, field.name)
          ? state.settingsDirty[field.name]
          : settingsValueToString(field.value);
      });
    });

    postJson("/api/settings/theme-preview", wanted).then(function (res) {
      if (!res.ok) { return; }
      advert.innerHTML = renderIrcLine(res.data.advert);
      notice.innerHTML = renderIrcLine(res.data.notice);
    }).catch(function () {
      advert.textContent = "The preview could not be loaded.";
      notice.textContent = "";
    });
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
    if (category.id === "appearance") {
      // ABOVE the fields. The colours are the subject and the sample is what
      // the operator is actually looking at while they change them; putting
      // it under six inputs would mean scrolling away from the thing being
      // adjusted to see what it did.
      html = themePreviewHtml() + html;
    }
    if (category.id === SERVED_FOLDERS_CATEGORY) {
      // ABOVE the fields, because the served library is what an operator comes
      // to this category for and Music directory is now only the fallback
      // used when nothing else is configured.
      //
      // ONE editor, not two. With more than one list configured the folders
      // live inside the lists, so showing the single-list folder editor as
      // well would be a second place to edit folders that quietly does
      // nothing. The button below is what moves an operator between them.
      if (state.listsSource === "file") {
        html = listsSectionHtml() + html;
      } else {
        html = foldersSectionHtml() +
          '<div class="served-folder-actions">' +
            '<button type="button" class="btn served-list-start">' +
            "Serve more than one list\u2026</button>" +
          "</div>" + html;
      }
    }
    if (category.id === ON_CONNECT_CATEGORY) {
      // BELOW the fields. What to send at registration is read after the
      // server and the nick it applies to, not before them.
      html += onConnectSectionHtml();
    }
    el.settingsFields.innerHTML = html;
    // The panel this repaints is where a carried confirmation has to land -
    // see applySettingsFlash(). Called here rather than in loadSettings()
    // because this is the point at which the new note element exists.
    applySettingsFlash();
    if (category.id === SERVED_FOLDERS_CATEGORY) {
      if (state.listsSource === "file") { attachListRows(); } else { attachFolderRows(); }
    }
    if (category.id === ON_CONNECT_CATEGORY) {
      attachOnConnectRows();
    }
    if (category.id === "appearance") {
      // The panel is inserted saying "Loading" and nothing else would fill
      // it: the refresh below otherwise runs only on an edit, so an operator
      // who opened the category and changed nothing would sit looking at a
      // sample that never arrived.
      refreshThemePreview();
    }

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
        var stored = dirty ? state.settingsDirty[field.name]
                           : settingsValueToString(field.value);
        // settingsDirty holds BYTES, like the baseline and the payload, so
        // the dirty comparison and the save body need no unit knowledge at
        // all. The division happens here and the multiplication happens in
        // onSettingsFieldChange - the two ends of the display, and nowhere
        // else.
        input.value = field.unit ? bytesToUnit(stored, field.unit_factor) : stored;
      }
      // "input" for anything typed into, so the save bar tracks a keystroke at
      // a time; "change" for the controls that have no intermediate state.
      var discrete = input.type === "checkbox" || input.tagName === "SELECT";
      input.addEventListener(discrete ? "change" : "input", onSettingsFieldChange);
    });

    // The colour pickers carry `data-irc-part`, not `data-setting`, so the
    // pass above leaves them alone: two selects mean one setting between
    // them, and neither one's value is the value to record.
    attachIrcColourPickers();
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
  // Set just before a reload that repaints the settings panel, and applied
  // once the new panel exists. Without it a confirmation written before the
  // reload is painted over by the reload itself.
  function applySettingsFlash() {
    var flash = state.settingsFlash;
    if (!flash) { return; }
    state.settingsFlash = null;
    var note = document.querySelector(".settings-password-note")
            || document.querySelector(".settings-note");
    if (!note) { return; }
    note.textContent = flash.text;
    note.className = flash.className;
  }

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
        loadFolders();
        loadLists();
        loadOnConnect();

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
  // Delegated for the same reason as the password toggle below: the folder
  // rows only exist after the paths category has been rendered, and they are
  // rebuilt from scratch every time it is.
  el.settingsFields.addEventListener("click", function (evt) {
    var browseButton = evt.target.closest(
      ".served-folder-browse, .browse-entry, .browse-up, .browse-use, .browse-close");
    if (browseButton) {
      var open = state.browse;
      if (browseButton.classList.contains("served-folder-browse")) {
        var rowEl = browseButton.closest(".served-folder-row");
        var index = parseInt(rowEl.dataset.servedFolderIndex, 10);
        var entryRow = state.foldersDraft[index];
        if (entryRow) { openBrowse(entryRow, entryRow.path || ""); }
      } else if (browseButton.classList.contains("browse-close")) {
        state.browse = null;
        renderSettingsCategory();
      } else if (browseButton.classList.contains("browse-up")) {
        openBrowse(open.row, open.parent || "");
      } else if (browseButton.classList.contains("browse-entry")) {
        // By index, never by a path read out of an attribute.
        var entry = open.entries[parseInt(browseButton.dataset.browseIndex, 10)];
        if (entry) { openBrowse(open.row, entry.path); }
      } else if (browseButton.classList.contains("browse-use")) {
        var target = open.row;
        // Still in the draft? The row can have been removed while the panel
        // was open, and writing into an orphaned object would look like it
        // worked while changing nothing on screen.
        if (target && state.foldersDraft.indexOf(target) !== -1) {
          target.path = open.path;
          if (!target.name) {
            // The label the server would derive anyway, shown now so the
            // operator can change it before saving rather than after.
            var parts = open.path.replace(/[\\/]+$/, "").split(/[\\/]/);
            target.name = parts[parts.length - 1] || open.path;
          }
        }
        state.browse = null;
        state.foldersNote = null;
        renderSettingsCategory();
      }
      return;
    }

    // #26's own buttons first: they share the settings pane with the folder
    // ones and a shared handler would have to tell them apart anyway.
    if (evt.target.closest(".on-connect-save")) {
      saveOnConnect();
      return;
    }

    var listButton = evt.target.closest(
      ".served-list-add, .served-list-save, .served-list-remove, " +
      ".list-folder-add, .list-folder-remove, .served-list-start");
    if (listButton) {
      handleListButton(listButton);
      return;
    }

    var button = evt.target.closest(".served-folder-add, .served-folder-save, .served-folder-remove, .served-folder-up, .served-folder-down");
    if (!button) { return; }
    var draft = state.foldersDraft || (state.foldersDraft = []);

    if (button.classList.contains("served-folder-add")) {
      draft.push({ name: "", path: "" });
    } else if (button.classList.contains("served-folder-save")) {
      saveFolders();
      return;
    } else {
      var row = button.closest(".served-folder-row");
      var index = parseInt(row.dataset.servedFolderIndex, 10);
      if (button.classList.contains("served-folder-remove")) {
        draft.splice(index, 1);
      } else if (button.classList.contains("served-folder-up") && index > 0) {
        draft.splice(index - 1, 0, draft.splice(index, 1)[0]);
      } else if (button.classList.contains("served-folder-down") && index < draft.length - 1) {
        draft.splice(index + 1, 0, draft.splice(index, 1)[0]);
      }
    }
    state.foldersNote = null;
    renderSettingsCategory();
  });

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
        state.settingsAdminPasswordSet = true;
        form.style.display = "none";
        // THE RELOAD REPAINTS THE PANEL THIS NOTE LIVES IN, so writing the
        // note first and reloading second showed it for one frame and then
        // destroyed it - the operator changed their password and saw nothing
        // confirm it. Carried across the repaint instead, and applied by
        // renderSettings() once the new panel exists.
        state.settingsFlash = {
          text: "Password changed. Rehashing…",
          className: "settings-note settings-password-note is-success"
        };
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
  loadNotices(false);
  loadMessages(false);
  setInterval(function () {
    // Keep the sidebar status fresh always; refresh the visible table only
    // when it is the one showing, so a search result is never clobbered by a
    // background poll.
    // Same panel, same tick. The list underneath is redrawn only when it is
    // the view on screen, for the reason the queue table gives just below.
    loadNotices(state.active === "notices");
    loadMessages(state.active === "messages");
    fetchJson("/api/queue").then(function (rows) {
      markConnection(true);
      renderSidebarStatus(rows);
      // The queue table lives on Stats now (#133). Same rule as before -
      // refresh the visible table only when it is the one showing, so a
      // background poll never clobbers what the operator is reading.
      if (state.active === "stats") {
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

  // Runs continuously regardless of which view is active, the same as
  // loadDownloads above: the buffer this polls (webserver._console_log) is
  // bounded server-side either way, and a console that is already caught up
  // when the operator switches to it is worth more than the handful of
  // requests saved by only polling while the tab is visible.
  pollConsoleLog();
  consoleLogTimer = setInterval(pollConsoleLog, CONSOLE_LOG_POLL_MS);
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
    // Full name on hover - the name column now truncates with an ellipsis
    // (.stat-top-table td:first-child) so two of these fit side by side.
    // Set as a PROPERTY, not concatenated into the markup above:
    // escapeHtml() leaves a double quote alone, so a filename containing one
    // could break out of a title="..." attribute built that way.
    var cells = node.querySelectorAll("td:first-child");
    for (var i = 0; i < cells.length; i++) {
      cells[i].title = rows[i].name;
    }
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

  // -------------------------------------------------------------- Console
  //
  // Two sources feed the same on-screen log: the ambient debug stream,
  // polled from GET /api/console/log (matching webserver.py's own LOG/COMMAND
  // split - see build_console_log_payload()'s docstring), and a command's own
  // reply, appended straight from what POST /api/console/command returns
  // rather than waiting for the next poll. Both render through the same
  // appendConsoleLines(), so the transcript reads as one continuous console
  // regardless of which endpoint a given line actually came from.

  function consoleLineNode(text, category, timeSeconds, isLocal) {
    var row = document.createElement("div");
    row.className = "console-line" + (isLocal ? " is-local" : "");
    if (category) { row.setAttribute("data-category", category); }

    var stamp = document.createElement("span");
    stamp.className = "console-line-time";
    stamp.textContent = timeSeconds
      ? new Date(timeSeconds * 1000).toLocaleTimeString()
      : "";

    var body = document.createElement("span");
    body.className = "console-line-text";
    body.textContent = text;

    row.appendChild(stamp);
    row.appendChild(body);
    return row;
  }

  function appendConsoleLines(rows) {
    if (!rows.length) { return; }
    // Scrolled to (or near) the bottom already? Stay pinned there as new
    // lines arrive. Already scrolled up reading something older? Leave the
    // view alone rather than yanking it back down mid-read.
    var atBottom = el.consoleLog.scrollHeight - el.consoleLog.scrollTop
                   - el.consoleLog.clientHeight < 24;
    rows.forEach(function (row) { el.consoleLog.appendChild(row); });

    // THE SERVER CAPS ITS BUFFER; THE BROWSER DID NOT. The poll appends
    // forever, three elements per line, and nothing removed any - so a
    // console left open overnight grows without bound in the one place
    // nobody restarts. Trimmed from the front, which is also the end the
    // reader has stopped caring about.
    while (el.consoleLog.childElementCount > CONSOLE_LOG_MAX_ELEMENTS) {
      el.consoleLog.removeChild(el.consoleLog.firstElementChild);
    }

    if (atBottom) { el.consoleLog.scrollTop = el.consoleLog.scrollHeight; }
  }

  // WEBUI_CONSOLE_ENABLED is off: the routes answer 404 and there is nothing
  // here to show. Take the page's Console away and stop asking.
  //
  // Handled as its own case rather than falling into the catch below, because
  // that one calls markConnection(false) - so a dashboard with the Console
  // switched off would have reported the whole daemon as unreachable, once
  // per poll, for ever. The bot is perfectly fine; one feature is not enabled.
  function disableConsoleUi() {
    if (consoleLogTimer !== null) {
      clearInterval(consoleLogTimer);
    }
    // #439: NOT stopped for good - swapped to the slow recheck cadence, so
    // this tab notices on its own once the Console is turned back on rather
    // than needing an F5. See enableConsoleUiIfNeeded() for the other half.
    consoleLogTimer = setInterval(pollConsoleLog, CONSOLE_LOG_RECHECK_MS);
    // HIDDEN, not removed - and #view-console stays in the DOM.
    //
    // activateView() walks every key in `views` and calls
    // getElementById("view-" + key).classList on each, so deleting the console
    // section made that return null and throw. On EVERY view switch. The
    // Console went away and took the rest of the navigation with it: Settings
    // sat on its "Loading" placeholder for ever, because the exception fired
    // before the branch that calls loadSettings(), and Queue, Stats and
    // Downloads stopped refreshing for the same reason.
    //
    // Found on a real install running with the Console off - which is the
    // default, so this was the ordinary case and not a corner of it.
    var navButton = document.querySelector(".nav-item[data-view=\"console\"]");
    if (navButton) { navButton.hidden = true; }
    if (state.active === "console") { activateView("search"); }
  }

  // #439: the other half of disableConsoleUi()'s slow recheck. A 200 here
  // proves the Console is live right now, whether or not this tab ever saw
  // it disabled - checked against the nav button's own hidden state rather
  // than a separate flag, so there is exactly one source of truth for
  // "currently disabled". Only does anything on the FIRST such response:
  // re-arming an already-fast timer or un-hiding an already-visible button
  // on every ordinary 2-second poll would be pure churn.
  function enableConsoleUiIfNeeded() {
    var navButton = document.querySelector(".nav-item[data-view=\"console\"]");
    if (!navButton || !navButton.hidden) { return; }
    navButton.hidden = false;
    clearInterval(consoleLogTimer);
    consoleLogTimer = setInterval(pollConsoleLog, CONSOLE_LOG_POLL_MS);
  }

  function pollConsoleLog() {
    fetchJson("/api/console/log?since=" + state.consoleCursor).then(function (payload) {
      markConnection(true);
      enableConsoleUiIfNeeded();
      state.consoleCursor = payload.cursor;
      appendConsoleLines((payload.lines || []).map(function (line) {
        return consoleLineNode(line.text, line.category, line.time, false);
      }));
    }).catch(function (err) {
      if (err && String(err.message) === "HTTP 404") {
        disableConsoleUi();
        return;
      }
      markConnection(false);
    });
  }

  el.consoleForm.addEventListener("submit", function (evt) {
    evt.preventDefault();
    var command = el.consoleInput.value.trim();
    if (!command) { return; }

    appendConsoleLines([consoleLineNode("> " + command, null, Date.now() / 1000, true)]);
    el.consoleInput.value = "";
    el.consoleInput.disabled = true;
    el.consoleRunBtn.disabled = true;

    postJson("/api/console/command", { command: command }).then(function (res) {
      // 200 carries {lines: [...]}; the one non-2xx case (an empty command,
      // rejected by build_console_command_result() before it ever reaches
      // adminchat.handle_command()) carries {error: "..."} instead.
      var lines = (res.data && res.data.lines) ||
                  (res.data && res.data.error ? [res.data.error] : []);
      appendConsoleLines(lines.map(function (text) {
        return consoleLineNode(text, null, Date.now() / 1000, true);
      }));
    }).catch(function (err) {
      appendConsoleLines([consoleLineNode(
        "Request failed: " + err.message, null, Date.now() / 1000, true)]);
    }).then(function () {
      el.consoleInput.disabled = false;
      el.consoleRunBtn.disabled = false;
      el.consoleInput.focus();
    });
  });

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
