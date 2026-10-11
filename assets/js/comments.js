/*
 * Giscus 评论区加载器（全站共用）
 * 使用方式：
 *   1. 仓库开启 GitHub Discussions，并安装 Giscus App（https://github.com/apps/giscus）
 *   2. 打开 https://giscus.app ，填入你的仓库，复制生成的 repo-id / category-id
 *   3. 把下面的 CONFIG 占位符替换成真实值即可，全站自动生效
 * 注意：仓库需为「公开」别人才能评论；未配置前评论区会显示提示，属正常。
 */
(function () {
  var CONFIG = {
    repo: "OWNER/REPO",        // 例如 "yimu-dev/yimu-dev.github.io"
    repoId: "REPO_ID",         // giscus.app 生成的 repository ID
    category: "Announcements", // 对应 Discussions 分类名
    categoryId: "CATEGORY_ID", // giscus.app 生成的 category ID
    mapping: "pathname",       // 每页按路径生成独立评论线程
    strict: "0",
    reactionsEnabled: "1",
    emitMetadata: "0",
    inputPosition: "bottom",
    theme: "light",
    lang: "zh-CN"
  };

  // 占位符未替换时不加载，避免控制台报错
  if (CONFIG.repoId === "REPO_ID" || CONFIG.categoryId === "CATEGORY_ID") {
    var tip = document.createElement("p");
    tip.style.color = "#8a909c";
    tip.style.fontSize = "13.5px";
    tip.textContent = "评论区待配置：在 assets/js/comments.js 填入 Giscus 的 repo-id 与 category-id 后即可启用。";
    var mount = document.getElementById("giscus-comments");
    if (mount) mount.appendChild(tip);
    return;
  }

  var s = document.createElement("script");
  s.src = "https://giscus.app/client.js";
  s.setAttribute("data-repo", CONFIG.repo);
  s.setAttribute("data-repo-id", CONFIG.repoId);
  s.setAttribute("data-category", CONFIG.category);
  s.setAttribute("data-category-id", CONFIG.categoryId);
  s.setAttribute("data-mapping", CONFIG.mapping);
  s.setAttribute("data-strict", CONFIG.strict);
  s.setAttribute("data-reactions-enabled", CONFIG.reactionsEnabled);
  s.setAttribute("data-emit-metadata", CONFIG.emitMetadata);
  s.setAttribute("data-input-position", CONFIG.inputPosition);
  s.setAttribute("data-theme", CONFIG.theme);
  s.setAttribute("data-lang", CONFIG.lang);
  s.crossOrigin = "anonymous";
  s.async = true;

  var mount = document.getElementById("giscus-comments");
  (mount || document.body).appendChild(s);
})();
