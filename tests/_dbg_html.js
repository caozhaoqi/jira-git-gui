const fs = require('fs');
const s = fs.readFileSync('/Users/caozhaoqi/PycharmProjects/jira-git-gui/web/hcm-meta.html', 'utf8');
const hits = [];
let i = -1;
while ((i = s.indexOf('script>', i + 1)) !== -1) hits.push(i);
console.log('script> hits (last 8):', hits.slice(-8));
const p = hits[hits.length - 2];
if (p !== undefined) {
  console.log('detail close context:', JSON.stringify(s.slice(p - 14, p + 10)));
  const slice = s.slice(p - 8, p + 8);
  const codes = Array.from(slice).map((c) => c.charCodeAt(0).toString(16).padStart(4, '0')).join(' ');
  console.log('char codes:', codes);
}
console.log('global idx literal </script> =', s.indexOf('</script>'));
console.log('global idx <\\/script  =', s.indexOf('<\\/script'));
console.log('global idx <\\\\/script =', s.indexOf('<\\\\/script'));
