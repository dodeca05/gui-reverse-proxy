// 관리자 화면 보조 스크립트 (CSP: script-src 'self')
document.addEventListener("DOMContentLoaded", () => {
  // 삭제 등 위험한 동작 확인
  document.querySelectorAll("form[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (!window.confirm(form.dataset.confirm)) event.preventDefault();
    });
  });

  // UTC 시각을 브라우저 지역 시간으로 표시
  const fmt = new Intl.DateTimeFormat(undefined, {
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  });
  document.querySelectorAll("time[datetime]").forEach((el) => {
    const date = new Date(el.getAttribute("datetime"));
    if (!Number.isNaN(date.getTime())) {
      el.title = el.textContent.trim();
      el.textContent = fmt.format(date);
    }
  });
});
