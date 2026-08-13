// Reports the numbers and media-query results this device actually has, so a
// layout bug can be diagnosed from evidence instead of from assumptions about
// what "an iPhone 7 viewport" should be. Plain static page: it deliberately
// does not load the app bundle, so nothing in the app's own CSS can distort
// what it measures.
(function () {
  function render() {
    var queries = [
      "(max-width: 850px)",
      "(max-width: 900px) and (orientation: landscape)",
      "(max-width: 480px)",
      "(max-height: 500px)",
      "(orientation: landscape)",
    ];
    var rows = [
      ["window.innerWidth x innerHeight", window.innerWidth + " x " + window.innerHeight],
      ["screen.width x height", screen.width + " x " + screen.height],
      ["devicePixelRatio", String(window.devicePixelRatio)],
      [
        "documentElement client",
        document.documentElement.clientWidth + " x " + document.documentElement.clientHeight,
      ],
      ["standalone (home-screen app)", String(!!navigator.standalone)],
    ];
    var html = "";
    for (var i = 0; i < rows.length; i++) {
      html +=
        '<div class="row"><span class="k">' +
        rows[i][0] +
        '</span><span class="v">' +
        rows[i][1] +
        "</span></div>";
    }
    for (var j = 0; j < queries.length; j++) {
      var m = window.matchMedia(queries[j]).matches;
      html +=
        '<div class="row"><span class="k">' +
        queries[j] +
        '</span><span class="v ' +
        (m ? "yes" : "no") +
        '">' +
        (m ? "MATCHES" : "no") +
        "</span></div>";
    }
    html +=
      '<div class="row"><span class="k">UA</span><span class="v" style="font-size:10px;font-weight:400">' +
      navigator.userAgent +
      "</span></div>";
    document.getElementById("out").innerHTML = html;
  }
  render();
  window.addEventListener("resize", render);
  window.addEventListener("orientationchange", function () {
    setTimeout(render, 300);
  });
})();
