/* ============================================================================
   ui.js — 三个页面共用的交互层
   ----------------------------------------------------------------------------
   职责只有五件事，都不依赖后端：
     1. 主题（日读 / 夜读两档，不跟随系统）与字号，存 localStorage
     2. Toast 提示
     3. 顶部金色进度条（生成答案时的不确定进度）
     4. 回到顶部按钮
     5. 快捷键面板

   主题必须在 <head> 里同步执行，不能等 DOMContentLoaded ——
   等 DOM 就绪再套 data-theme，用户会先看到一瞬白底（FOUC），
   深色模式下那一下很刺眼。所以本文件末尾立刻 applyTheme()。
   ========================================================================== */
(function () {
  'use strict';

  var THEME_KEY = 'law.theme';      // light | dark（只有这两档，没有 system）
  var FS_KEY = 'law.fs';            // s | m | l
  var root = document.documentElement;

  function read(k, d) {
    try { return localStorage.getItem(k) || d; } catch (e) { return d; }
  }
  function write(k, v) {
    try { localStorage.setItem(k, v); } catch (e) { /* 隐私模式忽略 */ }
  }

  /* -------------------------------------------------------------- 主题 */
  /* 只有两档之后这里就是「非 dark 即 light」。留着这个函数是因为
     applyTheme() 的入参来自 localStorage，脏值（旧版本写进去的 'system'）
     必须有个兜底，不能把非法值直接写到 <html data-theme> 上。 */
  function resolved(mode) {
    return mode === 'dark' ? 'dark' : 'light';
  }

  function applyTheme(mode, animate) {
    var t = resolved(mode);
    if (animate) {
      root.classList.add('theming');
      setTimeout(function () { root.classList.remove('theming'); }, 260);
    }
    root.setAttribute('data-theme', t);
    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute('content', t === 'dark' ? '#16120e' : '#f6f1e6');
    document.querySelectorAll('[data-theme-icon]').forEach(function (n) {
      var dark = (t === 'dark');
      var tip = dark ? '切到日读（浅色）' : '切到夜读（深色）';
      /* v127：顶栏换成了开关（input），设置弹窗里仍是按钮。两种都挂在
         data-theme-icon 上，这里按标签分派——给 input 写 textContent
         什么也看不见，还会把它的 value 弄脏。 */
      if (n.tagName === 'INPUT') {
        n.checked = dark;
      } else {
        // 用「日 / 夜」两个汉字而不是 ☀ / ☾：Segoe UI 里那两个符号没有字形，
        // 会退化成方框或者别的字体里的怪符号（截图里显示成一个 C）。
        // 汉字在任何中文字体里都有，而且和法典调性更搭。
        n.textContent = dark ? '日' : '夜';
        n.setAttribute('title', tip);
      }
      n.setAttribute('aria-label', tip);
    });
    /* 设置弹窗里那行「配色」把当前档位写出来（v130）。两档就把两档的名字摆在
       明面上——只写「切换」时，用户会怀疑背后还有一档（旧版本确实是三态，
       第三档叫「跟随系统」，那正是被投诉的东西）。这里跟 data-theme 走，
       不另存一份状态。 */
    document.querySelectorAll('[data-theme-now]').forEach(function (n) {
      n.textContent = (t === 'dark') ? '夜读' : '日读';
    });
    return t;
  }

  /* 只有浅/深两档，系统的 prefers-color-scheme 一眼都不看。
     曾经的做法是「第一次进来按系统偏好落一次，再写回 localStorage」——想法
     是第一次别太刺眼，实际效果是「我明明选了日读，换台机器/清一次站点数据
     又自己变回夜读」，读起来就是还在跟随系统。现在没有值得猜的：没有存过
     就是日读，改过一次之后就只认用户最后点的那个值。
     旧版本写进去的 'system' / 空值 / 任何脏值同样落回日读。 */
  function currentMode() {
    var m = read(THEME_KEY, '');
    if (m === 'dark' || m === 'light') return m;
    write(THEME_KEY, 'light');
    return 'light';
  }

  /* 设置弹窗里那个「切换」按钮走这里。动作和顶栏开关完全一样，只多一句
     提示——弹窗关掉之后看不到开关的位置，光看页面变色容易以为是加载完成。 */
  function cycleTheme() {
    var next = toggleTheme(null);
    toast('配色：' + (next === 'dark' ? '夜读' : '日读'));
    return next;
  }

  /* 顶栏开关走这里：点一下就是明确指定另一档。 */
  function anchorPoint(node) {
    var r = node && node.getBoundingClientRect ? node.getBoundingClientRect() : null;
    var x = (r && r.width) ? r.left + r.width / 2 : window.innerWidth - 42;
    var y = (r && r.height) ? r.top + r.height / 2 : 40;
    var dx = Math.max(x, window.innerWidth - x);
    var dy = Math.max(y, window.innerHeight - y);
    return { x: x, y: y, r: Math.sqrt(dx * dx + dy * dy) + 24 };
  }

  function reduced() {
    return !!(window.matchMedia &&
              window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }

  /* 深浅切换的过渡和新法域的扩散圆共用 ::view-transition 那套规则，只是
     更快（380ms）——换法是「换一个世界」，主题是「换个光线」，两者等长
     会让切主题显得很拖。 */
  function animateTheme(next, from) {
    if (!document.startViewTransition || reduced()) { applyTheme(next, true); return; }
    var p = anchorPoint(from);
    root.style.setProperty('--vc-x', p.x + 'px');
    root.style.setProperty('--vc-y', p.y + 'px');
    root.style.setProperty('--vc-r', p.r + 'px');
    root.style.setProperty('--vc-dur', '380ms');
    root.style.setProperty('--vc-ease', 'cubic-bezier(.34,.02,.16,1)');
    // 换法是「换一个世界」，暗色下要压暗旧帧补对比；换主题就是「换光线」，
    // 旧帧再压暗会变成闪一下黑，所以这里显式拉回 1。
    root.style.setProperty('--vc-old-dim', '1');
    /* v131：换主题这一趟，旧帧一格都不许动。
       之前只把 --vc-old-dim 拉回 1，漏了 vc-out 的另一半——它还会把旧帧
       淡到 88% 不透明度（--vc-old-op 全项目从来没人设过，恒用兜底值）。
       于是圆还没盖到的地方整页先暗 12%、再被圆盖掉，读起来就是「切换的时候
       闪一下 / 震一下」。换法需要这层淡出（新旧要有前后关系），换主题不需要：
       圆外本来就该原样不动，只有圆里面在换色。
       直接把它动画名换成 none —— 不淡、不压暗、不提升滤镜层。顺带解决另一个
       隐患：filter:brightness(1) 看着是空操作，仍要让整页走一遍滤镜光栅化，
       个别显卡上会把合成层偏移取整，出来就是很细的抖动。 */
    root.style.setProperty('--vc-out-name', 'none');
    root.style.setProperty('--vc-old-op', '1');
    /* 换法的 620ms 过场里按 Ctrl+J：那个描边环还挂着 view-transition-name，
       主题这一趟会把它一起快照进去，屏幕上凭空多出一个圆环。先摘掉——
       环是换法专属的东西，主题换的是光线，不该带它。
       这一下也会把换法的 VT cancel 掉，收尾由 index.html 里 switchProfile
       的 settle 负责。 */
    var ring = document.getElementById('vc-ring');
    if (ring) { ring.style.viewTransitionName = ''; ring.style.opacity = '0'; }
    /* .theming 把 background-color 那类 transition 全关掉。不关的话新帧可能
       被截在「过渡到一半」的颜色上，圆铺满之后整页还会自己再变一次色。 */
    root.classList.add('theming');
    var vt = document.startViewTransition(function () { applyTheme(next, false); });
    var done = function () {
      setTimeout(function () { root.classList.remove('theming'); }, 40);
    };
    vt.finished.then(done).catch(done);
  }

  function toggleTheme(from) {
    var now = resolved(currentMode());
    var next = now === 'dark' ? 'light' : 'dark';
    write(THEME_KEY, next);
    animateTheme(next, from);
    return next;
  }

  /* 不监听系统配色变化，连「第一次按系统定」都不做（见 currentMode）。
     这里没有监听器可删了：主题只有一个来源——用户点的那个开关。 */

  /* -------------------------------------------------------------- 字号 */
  function applyFs(v, animate) {
    if (animate) {
      root.classList.add('theming');
      setTimeout(function () { root.classList.remove('theming'); }, 260);
    }
    root.setAttribute('data-fs', v);
  }

  function cycleFs() {
    var order = ['m', 'l', 's'];
    var now = read(FS_KEY, 'm');
    var next = order[(order.indexOf(now) + 1) % order.length] || 'm';
    write(FS_KEY, next);
    applyFs(next, true);
    toast('字号：' + ({ s: '小', m: '标准', l: '大' })[next]);
  }

  /* -------------------------------------------------------------- Toast */
  var toastBox = null;
  function toast(text, kind) {
    if (!document.body) return;
    if (!toastBox) {
      toastBox = document.createElement('div');
      toastBox.className = 'law-toasts no-print';
      document.body.appendChild(toastBox);
    }
    var t = document.createElement('div');
    t.className = 'law-toast' + (kind ? ' ' + kind : '');
    t.textContent = text;
    toastBox.appendChild(t);
    // 入场用类切换而不是直接改 style：动画交给 CSS，JS 只负责生死
    requestAnimationFrame(function () { t.classList.add('in'); });
    setTimeout(function () {
      t.classList.remove('in');
      setTimeout(function () { if (t.parentNode) t.parentNode.removeChild(t); }, 260);
    }, 2600);
  }

  /* -------------------------------------------------------------- 进度条 */
  var bar = null, barTimer = null;
  function progressStart() {
    if (!document.body) return;
    if (!bar) {
      bar = document.createElement('div');
      bar.className = 'law-progress no-print';
      bar.innerHTML = '<i></i>';
      document.body.appendChild(bar);
    }
    bar.classList.add('on');
    var i = bar.firstChild;
    var p = 8;
    i.style.width = p + '%';
    clearInterval(barTimer);
    // 不确定进度：越接近 90% 走得越慢，永远不到 100——真做完才归位
    barTimer = setInterval(function () {
      p += (90 - p) * 0.12;
      i.style.width = p.toFixed(1) + '%';
    }, 260);
  }
  function progressDone() {
    if (!bar) return;
    clearInterval(barTimer);
    bar.firstChild.style.width = '100%';
    setTimeout(function () { bar.classList.remove('on'); }, 320);
    setTimeout(function () { if (bar && bar.firstChild) bar.firstChild.style.width = '0'; }, 700);
  }

  /* -------------------------------------------------------------- 工具 */
  function readCookie(name) {
    var parts = ('; ' + document.cookie).split('; ' + name + '=');
    return parts.length === 2 ? parts.pop().split(';').shift() : '';
  }

  /* CSRF 双提交：服务端在 cookie 里放一个随机串，写请求必须把它原样抄到
     请求头。跨站页面读不到同源 cookie，所以抄不出来。

     为什么包一层 fetch 而不是在每个调用点加 header：
     三个页面加起来二十多处 fetch，逐个加必然漏一处，而漏的那处
     平时不报错、只有被跨站攻击时才暴露。包一层是一处生效。 */
  (function patchFetch() {
    var raw = window.fetch;
    if (!raw || raw.__csrfPatched) return;
    var wrapped = function (input, init) {
      init = init || {};
      var m = String(init.method || 'GET').toUpperCase();
      if (m !== 'GET' && m !== 'HEAD' && m !== 'OPTIONS') {
        var t = readCookie('law_csrf');
        if (t) {
          var h = new Headers(init.headers || {});
          h.set('X-CSRF-Token', t);
          init.headers = h;
        }
      }
      return raw.call(window, input, init);
    };
    wrapped.__csrfPatched = true;
    window.fetch = wrapped;
  })();

  function copy(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () {
        toast('已复制', 'ok');
      }, function () { fallbackCopy(text); });
    } else {
      fallbackCopy(text);
    }
  }

  function fallbackCopy(text) {
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.style.cssText = 'position:fixed;opacity:0;left:-9999px';
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); toast('已复制', 'ok'); }
    catch (e) { toast('复制失败，请手动选中', 'bad'); }
    document.body.removeChild(ta);
  }

  function download(filename, text) {
    var blob = new Blob(['\ufeff' + text], { type: 'text/plain;charset=utf-8' });
    var a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    setTimeout(function () {
      URL.revokeObjectURL(a.href);
      if (a.parentNode) a.parentNode.removeChild(a);
    }, 200);
    toast('已导出 ' + filename, 'ok');
  }

  /* 给容器里的子元素排队入场：延迟递增，超过 8 个就不再叠加，
     否则第 30 条要等 3 秒才出现，看着像卡住。 */
  function stagger(container, selector, step) {
    if (!container) return;
    var nodes = container.querySelectorAll(selector);
    for (var i = 0; i < nodes.length && i < 8; i++) {
      nodes[i].style.animationDelay = (i * (step || 45)) + 'ms';
    }
  }

  /* -------------------------------------------------------------- 快捷键面板 */
  var helpEl = null;
  var KEYS = [
    ['Enter', '发送提问（输入框内）'],
    ['Shift + Enter', '换行，不发送'],
    ['/ 或 Ctrl + K', '聚焦输入框'],
    ['Ctrl + N', '开始新对话'],
    ['Ctrl + B', '打开案情档案'],
    ['Alt + 1 … 7', '左侧七个入口直达'],
    ['Ctrl + J', '切换日读 / 夜读'],
    ['Ctrl + ↑ / ↓', '跳到顶部 / 底部'],
    ['Esc', '关闭弹层'],
    ['?', '打开这个面板']
  ];

  function openHelp() {
    if (!helpEl) {
      helpEl = document.createElement('div');
      helpEl.className = 'law-help no-print';
      helpEl.innerHTML =
        '<div class="law-help-sheet" role="dialog" aria-modal="true" aria-label="快捷键">' +
          '<h3>快捷键</h3><ul>' +
          KEYS.map(function (k) {
            return '<li><kbd>' + k[0] + '</kbd><span>' + k[1] + '</span></li>';
          }).join('') +
          '</ul>' +
          '<p class="law-help-tip">提示：顶栏的「夜 / 日」切换配色，A+ 调字号。</p>' +
          '<button class="btn ghost" data-close>知道了</button>' +
        '</div>';
      helpEl.addEventListener('click', function (e) {
        if (e.target === helpEl || e.target.hasAttribute('data-close')) closeHelp();
      });
      document.body.appendChild(helpEl);
    }
    helpEl.classList.add('on');
  }
  function closeHelp() { if (helpEl) helpEl.classList.remove('on'); }

  /* -------------------------------------------------------------- 回到顶部 */
  function mountScrollTop() {
    var b = document.createElement('button');
    b.className = 'law-top no-print';
    b.type = 'button';
    b.title = '回到顶部';
    b.setAttribute('aria-label', '回到顶部');
    b.innerHTML = '↑';
    b.addEventListener('click', function () {
      window.scrollTo({ top: 0, behavior: 'smooth' });
    });
    document.body.appendChild(b);
    var onScroll = function () {
      b.classList.toggle('on', window.scrollY > 420);
    };
    window.addEventListener('scroll', onScroll, { passive: true });
    onScroll();
  }

  /* -------------------------------------------------------------- 全局按键 */
  function bindKeys() {
    document.addEventListener('keydown', function (e) {
      var tag = (e.target.tagName || '').toLowerCase();
      var typing = (tag === 'input' || tag === 'textarea' || e.target.isContentEditable);

      if (e.key === 'Escape') { closeHelp(); return; }

      if (e.ctrlKey || e.metaKey) {
        var k = e.key.toLowerCase();
        // Ctrl+J 和顶栏开关必须是同一个动作：键走三态、开关走两态的话，
        // 按完快捷键开关的位置会和用户以为的状态错开一格。
        if (k === 'j') {
          e.preventDefault();
          toggleTheme(document.querySelector('.theme-sw') || null);
          return;
        }
        if (k === 'k') {
          e.preventDefault();
          var q = document.getElementById('q');
          if (q) q.focus();
          return;
        }
        if (k === 'n') {
          // 只有对话页会定义 LawUI.newChat
          if (typeof window.LawUI.onNewChat === 'function') {
            e.preventDefault();
            window.LawUI.onNewChat();
          }
          return;
        }
        if (e.key === 'ArrowUp') { e.preventDefault(); window.scrollTo({ top: 0, behavior: 'smooth' }); return; }
        if (e.key === 'ArrowDown') { e.preventDefault(); window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' }); return; }
      }

      if (typing) return;
      if (e.key === '/') {
        e.preventDefault();
        var q2 = document.getElementById('q');
        if (q2) q2.focus();
      } else if (e.key === '?') {
        e.preventDefault();
        openHelp();
      }
    });
  }

  /* ---------------------------------------------------------- 焦点陷阱 */
  /* 模态框打开时，Tab 不该跑到背后的页面上。
     这件事用鼠标永远发现不了，但用键盘的人第一下就撞上：对话框开着，
     Tab 三下焦点已经在顶栏了，再按 Enter 就点到了背面的按钮。

     另外两件必须做对的事（都是「打开时记、关闭时还」）：
       · 打开时把焦点移进去，否则读屏软件还停在原来的位置
       · 关闭时把焦点还给打开它的那个按钮，否则焦点会掉到 body 上，
         用户得从头 Tab 一遍才找得回来 */
  var traps = new WeakMap();
  /* 陷阱要能叠：案情面板里能打开申请书弹窗，两层都装着监听。
     没这个栈，下面那层的处理器会先跑到（先注册先触发），
     发现焦点不在自己里面，就把焦点抢回自己被压在底下的第一个按钮——
     于是申请书弹窗开着，按一下 Tab 光标跳到背后去了。 */
  var stack = [];
  var FOCUSABLE = 'a[href],button:not([disabled]),input:not([disabled]),' +
                  'select:not([disabled]),textarea:not([disabled]),' +
                  '[tabindex]:not([tabindex="-1"])';

  function visibleNodes(host) {
    return Array.prototype.filter.call(
      host.querySelectorAll(FOCUSABLE),
      function (n) { return n.getClientRects().length > 0; });
  }

  function trapFocus(host, opener) {
    if (!host || traps.has(host)) return;
    /* opener 是「谁打开的」，不传就取当前焦点。
       为什么要能传：触发按钮在异步期间常被 disabled（防止连点），
       而给已聚焦的元素设 disabled 会让浏览器当场把焦点丢到 <body>——
       等弹窗开出来再取 activeElement，取到的已经是 body 了。
       关闭时还焦点给 body 等于没还。 */
    var prev = opener || document.activeElement;
    var onKey = function (e) {
      if (e.key !== 'Tab') return;
      if (stack[stack.length - 1] !== host) return;   // 有更上层的模态在，交给它
      var list = visibleNodes(host);
      if (!list.length) { e.preventDefault(); return; }
      var first = list[0], last = list[list.length - 1];
      var inside = host.contains(document.activeElement);
      if (e.shiftKey) {
        if (!inside || document.activeElement === first) {
          e.preventDefault(); last.focus();
        }
      } else if (!inside || document.activeElement === last) {
        e.preventDefault(); first.focus();
      }
    };
    document.addEventListener('keydown', onKey, true);
    traps.set(host, { prev: prev, key: onKey });
    stack.push(host);

    // 等一帧再移焦点：调用方通常刚把 hidden 去掉，此刻元素还没布局，
    // 立刻 focus 会被浏览器忽略。
    setTimeout(function () {
      if (!traps.has(host)) return;         // 已经关了
      // 调用方自己已经指定了落点（改密码弹窗要停在「当前密码」框），别覆盖它
      if (host.contains(document.activeElement)) return;
      var auto = host.querySelector('[autofocus]');
      var list = visibleNodes(host);
      if (auto) { auto.focus(); return; }
      if (list.length) { list[0].focus(); return; }
      host.setAttribute('tabindex', '-1');
      host.focus();
    }, 40);
  }

  function untrapFocus(host) {
    var t = host && traps.get(host);
    if (!t) return;
    document.removeEventListener('keydown', t.key, true);
    traps.delete(host);
    var i = stack.indexOf(host);
    if (i >= 0) stack.splice(i, 1);
    // 焦点还回去。不还的话它会掉到 <body>，键盘用户要重新 Tab 一遍。
    try {
      if (t.prev && t.prev.focus && document.contains(t.prev)) t.prev.focus();
    } catch (e) { /* 元素已经不在 DOM 里了，忽略 */ }
  }

  /* -------------------------------------------------------------- 对外 */
  window.LawUI = {
    toast: toast,
    copy: copy,
    download: download,
    stagger: stagger,
    progressStart: progressStart,
    progressDone: progressDone,
    cycleTheme: cycleTheme,
    toggleTheme: toggleTheme,
    cycleFs: cycleFs,
    theme: function () { return resolved(currentMode()); },
    mode: currentMode,
    help: openHelp,
    // 页面要判断「帮助层是不是开着」。它是 ui.js 自己创建的节点，
    // 外面拿不到，只能问。
    helpOpen: function () { return !!(helpEl && helpEl.classList.contains('on')); },
    trapFocus: trapFocus,
    untrapFocus: untrapFocus,
    onNewChat: null
  };

  /* -------------------------------------------------------------- 立刻执行 */
  applyTheme(currentMode(), false);
  applyFs(read(FS_KEY, 'm'), false);

  function bootDom() {
    bindKeys();
    mountScrollTop();
    // 顶栏是开关，设置弹窗里是按钮；两者现在都是「浅 ↔ 深」同一个动作
    document.querySelectorAll('[data-theme-icon]').forEach(function (n) {
      n.addEventListener('click', function () {
        if (n.tagName === 'INPUT') toggleTheme(n.closest('.theme-sw') || n);
        else cycleTheme();
      });
    });
    var fsBtn = document.querySelector('[data-fs-btn]');
    if (fsBtn) fsBtn.addEventListener('click', cycleFs);
    var helpBtn = document.querySelector('[data-help-btn]');
    if (helpBtn) helpBtn.addEventListener('click', openHelp);
    applyTheme(currentMode(), false);      // 图标文案要跟着真实主题走
    document.body.classList.add('law-ready');
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bootDom);
  } else {
    bootDom();
  }
})();
