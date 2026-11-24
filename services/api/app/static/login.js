// A signed-out visit to /#/docs/docintel reaches the server as "/", so the form's next field has no route in it.
// The browser keeps the fragment across the redirect to /login, so add it here and the sign-in lands on that route.
(() => {
  const next = document.querySelector('input[name="next"]');
  if (next && window.location.hash && !next.value.includes("#")) next.value += window.location.hash;
})();
