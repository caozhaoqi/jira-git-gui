# tab-k8s 内再检查：逐个 key line depth
import pathlib
lines = pathlib.Path('web/index.html').read_text(encoding='utf-8').splitlines()
tk8 = 220
depth = 0
for i in range(tk8, 661):
    line = lines[i-1]
    depth += line.count('<div ') + line.count('<div>')
    depth -= line.count('</div>')
    if any(k in line for k in ['tab-k8s','tab-inner','k8s-sub-snapshot','k8s-sub-shell','k8s-sub-files','k8s-env-modal','k8s-describe-modal']):
        print(f"L{i}: d={depth:2d} | {line.strip()[:65]}")
print(f"L660 final (before tab-hcm): depth={depth}, expect=-2, diff={depth-(-2)}")
