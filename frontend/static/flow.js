(function () {
  const STATUS_LABELS = {
    pending: "Pending",
    running: "Running",
    done: "Done",
    failed: "Failed",
    skipped: "Skipped",
  };

  const tabsRoot = document.querySelector(".tabs");
  if (!tabsRoot) return;

  const runId = tabsRoot.dataset.runId;
  const statusUrl = `/runs/${runId}/status.json`;
  let pollTimer = null;

  function formatBytes(bytes) {
    if (bytes == null || Number.isNaN(bytes)) return "";
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }

  function formatMetricName(key) {
    return String(key).replaceAll("__", " · ").replaceAll("_", " ");
  }

  function updateStepDownloads(order, output) {
    const panel = document.getElementById(`tab-step-${order}`);
    if (!panel) return;

    const wrap = panel.querySelector("[data-step-downloads]");
    if (!wrap) return;

    const zipUrl = `/runs/${runId}/steps/${order}/outputs.zip`;
    const svgUrl = `/runs/${runId}/steps/${order}/preview.svg?download=1`;
    const sourceUrl = `/runs/${runId}/steps/${order}/preview-source`;
    const canZip = Boolean(
      output &&
        ((output.artifacts || []).length > 0 ||
          output.step_dir ||
          output.preview_svg)
    );
    const canSvg = Boolean(
      output && (output.preview_svg || output.preview_html)
    );
    const canSource = Boolean(
      output && (output.preview_svg || output.preview_html)
    );

    let zipBtn = wrap.querySelector(".step-download-zip");
    let svgBtn = wrap.querySelector(".step-download-svg");
    let sourceBtn = wrap.querySelector(".step-download-source");

    if (canZip && !zipBtn) {
      zipBtn = document.createElement("a");
      zipBtn.className = "btn step-download-zip";
      zipBtn.textContent = "Download all outputs (.zip)";
      zipBtn.setAttribute("download", "");
      wrap.appendChild(zipBtn);
    }
    if (zipBtn) {
      zipBtn.href = zipUrl;
      zipBtn.classList.toggle("hidden", !canZip);
    }

    if (canSvg && !svgBtn) {
      svgBtn = document.createElement("a");
      svgBtn.className = "btn step-download-svg";
      svgBtn.textContent = "Download preview (.svg)";
      svgBtn.setAttribute("download", "");
      wrap.appendChild(svgBtn);
    }
    if (svgBtn) {
      svgBtn.href = svgUrl;
      svgBtn.classList.toggle("hidden", !canSvg);
    }

    const sourceLabel = output?.preview_source?.name
      ? `Download layout source (${output.preview_source.name})`
      : "Download layout source";
    if (canSource && !sourceBtn) {
      sourceBtn = document.createElement("a");
      sourceBtn.className = "btn step-download-source";
      sourceBtn.setAttribute("download", "");
      wrap.appendChild(sourceBtn);
    }
    if (sourceBtn) {
      sourceBtn.href = sourceUrl;
      sourceBtn.textContent = sourceLabel;
      sourceBtn.classList.toggle("hidden", !canSource);
    }

    wrap.classList.toggle("hidden", !canZip && !canSvg && !canSource);
  }

  function hasOutputContent(output) {
    if (!output) return false;
    if (output.error) return true;
    return (
      output.elapsed_s != null ||
      (output.views_updated && output.views_updated.length > 0) ||
      (output.metrics && Object.keys(output.metrics).length > 0) ||
      Boolean(output.preview_html) ||
      Boolean(output.preview_svg) ||
      (output.artifacts && output.artifacts.length > 0)
    );
  }

  function renderStepOutput(container, output, summaryFallback) {
    if (!container) return;

    const panel = container.closest(".tab-panel");
    const stepOrder = panel ? panel.dataset.stepOrder : null;

    const wrap = container.closest(".step-summary-wrap");
    const fallback = wrap ? wrap.querySelector(".step-summary-fallback") : null;

    if (!hasOutputContent(output)) {
      if (summaryFallback) {
        container.innerHTML = "";
        if (fallback) {
          fallback.textContent = summaryFallback;
          fallback.classList.remove("hidden");
        } else {
          container.innerHTML = `<pre class="summary step-summary-fallback">${escapeHtml(summaryFallback)}</pre>`;
        }
        if (wrap) wrap.classList.remove("hidden");
      }
      return;
    }

    if (fallback) fallback.classList.add("hidden");

    if (output.error) {
      container.innerHTML = `<pre class="error">${escapeHtml(output.error)}</pre>`;
      if (wrap) wrap.classList.remove("hidden");
      return;
    }

    const parts = [];

    if (output.elapsed_s != null) {
      parts.push(
        `<p class="output-line"><strong>Time elapsed:</strong> ${output.elapsed_s}s</p>`
      );
    }

    if (output.views_updated && output.views_updated.length) {
      parts.push(
        `<div class="output-block"><strong>Views updated</strong><ul class="output-list">${output.views_updated
          .map((v) => `<li>${escapeHtml(v)}</li>`)
          .join("")}</ul></div>`
      );
    }

    if (output.preview_svg && stepOrder != null) {
      const previewUrl = `/runs/${runId}/steps/${stepOrder}/preview.svg`;
      parts.push(
        `<div class="output-block output-preview"><strong>Layout preview</strong>` +
          `<div class="preview-svg-wrap">` +
          `<object type="image/svg+xml" data="${previewUrl}" class="layout-preview-svg" aria-label="Layout preview"></object>` +
          `</div></div>`
      );
    } else if (output.preview_html) {
      parts.push(
        `<div class="output-block output-preview"><strong>Layout preview</strong>${output.preview_html}</div>`
      );
    }

    const metrics = output.metrics || {};
    const metricKeys = Object.keys(metrics);
    if (metricKeys.length) {
      parts.push(
        `<div class="output-block"><strong>Metrics</strong><table class="output-metrics"><thead><tr><th>Metric</th><th>Value</th></tr></thead><tbody>${metricKeys
          .map(
            (key) =>
              `<tr><td>${escapeHtml(formatMetricName(key))}</td><td><code>${escapeHtml(String(metrics[key]))}</code></td></tr>`
          )
          .join("")}</tbody></table></div>`
      );
    }

    const artifacts = output.artifacts || [];
    if (artifacts.length) {
      parts.push(
        `<div class="output-block"><strong>Output files</strong><ul class="output-artifacts">${artifacts
          .map(
            (a) =>
              `<li><code>${escapeHtml(a.path)}</code> <span class="meta">(${formatBytes(a.size)})</span></li>`
          )
          .join("")}</ul></div>`
      );
    }

    if (output.step_dir) {
      parts.push(
        `<p class="output-line meta"><strong>Step directory:</strong> <code>${escapeHtml(output.step_dir)}</code></p>`
      );
    }

    if (!parts.length && summaryFallback) {
      container.innerHTML = `<pre class="summary step-summary-fallback">${escapeHtml(summaryFallback)}</pre>`;
    } else {
      container.innerHTML = parts.join("");
    }

    if (wrap) wrap.classList.remove("hidden");
  }

  function escapeHtml(text) {
    return String(text)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }

  function loadInitialOutputs() {
    document.querySelectorAll(".step-summary-wrap").forEach((wrap) => {
      const container = wrap.querySelector("[data-step-output]");
      const dataEl = wrap.querySelector(".step-output-data");
      const fallbackEl = wrap.querySelector(".step-summary-fallback");
      const fallback = fallbackEl ? fallbackEl.textContent : "";

      if (!dataEl) return;
      try {
        const output = JSON.parse(dataEl.textContent || "{}");
        renderStepOutput(container, output, fallback);
        const order = Number(wrap.closest(".tab-panel")?.dataset.stepOrder);
        if (!Number.isNaN(order)) {
          updateStepDownloads(order, output);
        }
      } catch {
        if (fallback) renderStepOutput(container, null, fallback);
      }
    });
  }

  function activateTab(tabId) {
    document.querySelectorAll(".tab-btn").forEach((btn) => {
      const on = btn.dataset.tab === tabId;
      btn.classList.toggle("active", on);
      btn.setAttribute("aria-selected", on ? "true" : "false");
    });
    document.querySelectorAll(".tab-panel").forEach((panel) => {
      const on = panel.dataset.tab === tabId || panel.id === `tab-${tabId}`;
      panel.classList.toggle("active", on);
      if (on) {
        panel.removeAttribute("hidden");
      } else {
        panel.setAttribute("hidden", "");
      }
    });
    if (history.replaceState) {
      history.replaceState(null, "", `#${tabId}`);
    }
  }

  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => activateTab(btn.dataset.tab));
  });

  const hash = window.location.hash.replace("#", "");
  if (hash && document.getElementById(`tab-${hash}`)) {
    activateTab(hash);
  } else {
    activateTab("overview");
  }

  function setRunning(active, status) {
    tabsRoot.dataset.running = active ? "1" : "0";
    document
      .querySelectorAll(".step-run-btn, #btn-setup, #btn-run-all, #btn-delete-run")
      .forEach((el) => {
        el.disabled = active;
      });
    const hint = document.getElementById("poll-hint");
    if (!hint) return;
    if (!active) {
      hint.textContent = "";
      return;
    }
    if (status === "setting_up") {
      hint.textContent =
        "Configuring flow for this run… (PDK must already be downloaded by ./run.sh)";
    } else {
      hint.textContent = "Step running…";
    }
  }

  function updateSetupLog(setupLog) {
    const wrap = document.getElementById("setup-log-wrap");
    const el = document.getElementById("setup-log");
    if (!wrap || !el || !setupLog) return;
    el.textContent = setupLog;
    wrap.classList.remove("hidden");
  }

  function updateFromStatus(data) {
    const pill = document.getElementById("run-status-pill");
    if (pill) {
      pill.className = `status-pill status-${data.status}`;
      pill.textContent = data.status.replaceAll("_", " ");
    }

    const err = document.getElementById("run-error");
    if (err) {
      if (data.error_message) {
        err.textContent = data.error_message;
        err.classList.remove("hidden");
      } else {
        err.classList.add("hidden");
      }
    }

    data.steps.forEach((s) => {
      const panel = document.getElementById(`tab-step-${s.order}`);
      if (!panel) return;

      const tabBtn = document.querySelector(`.tab-btn[data-tab="step-${s.order}"]`);
      const card = panel.querySelector(".step-card");
      const statusEl = panel.querySelector("[data-step-status]");
      const logWrap = panel.querySelector(".step-log-wrap");
      const logEl = panel.querySelector(".step-log");
      const sumWrap = panel.querySelector(".step-summary-wrap");
      const outputEl = panel.querySelector("[data-step-output]");

      if (tabBtn) {
        const active = tabBtn.classList.contains("active");
        tabBtn.className = `tab-btn tab-btn-step status-${s.status}${active ? " active" : ""}`;
      }
      if (card) card.className = `step-card status-${s.status}`;
      if (statusEl) {
        statusEl.className = `status-pill status-${s.status}`;
        statusEl.textContent = STATUS_LABELS[s.status] || s.status;
      }
      if (s.log && logEl && logWrap) {
        logEl.textContent = s.log;
        logWrap.classList.remove("hidden");
      }
      if ((hasOutputContent(s.output) || s.summary)) {
        renderStepOutput(outputEl, s.output || {}, s.summary || "");
      }
      updateStepDownloads(s.order, s.output || {});
    });

    updateSetupLog(data.setup_log || "");
    setRunning(data.is_running, data.status);
  }

  function pollOnce() {
    fetch(statusUrl, { headers: { Accept: "application/json" } })
      .then((r) => r.json())
      .then((data) => {
        updateFromStatus(data);
        const stillBusy =
          data.is_running ||
          data.status === "setting_up" ||
          data.status === "running";
        if (stillBusy) {
          const delay = data.status === "setting_up" ? 2000 : 4000;
          pollTimer = setTimeout(pollOnce, delay);
        } else {
          pollTimer = null;
          const url = new URL(window.location.href);
          url.searchParams.delete("watch");
          if (history.replaceState) {
            history.replaceState(null, "", url.pathname + url.hash);
          }
        }
      })
      .catch(() => {
        pollTimer = null;
      });
  }

  function maybeStartPolling() {
    if (pollTimer) return;
    const shouldWatch =
      tabsRoot.dataset.watch === "1" || tabsRoot.dataset.running === "1";
    if (shouldWatch) {
      pollOnce();
    }
  }

  loadInitialOutputs();
  maybeStartPolling();
})();
