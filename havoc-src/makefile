ifndef VERBOSE
.SILENT:
endif

# main build target. compiles the teamserver only
# （Qt 客户端已移除；client/Modules 作为 Web 端的 Python 模块库保留，不参与编译）
all: ts-build

# teamserver building target
ts-build:
	@ echo "[*] building teamserver"
# 交叉编译器：本部署用 data/*-w64-mingw32-cross/bin 下的包装脚本，
# 缺失时才调用上游 Install.sh（那个脚本需要 sudo，且带 dir/ 路径 typo，会无条件 apt）。
	@ if [ ! -x data/x86_64-w64-mingw32-cross/bin/x86_64-w64-mingw32-gcc ] && \
	     [ ! -x data/i686-w64-mingw32-cross/bin/i686-w64-mingw32-gcc ]; then \
		echo "[!] 缺少交叉编译器，尝试 ./teamserver/Install.sh（需要 sudo）"; \
		./teamserver/Install.sh; \
	else \
		echo "[*] 交叉编译器已就绪（data/*-w64-mingw32-cross 包装脚本）"; \
	fi
	@ cd teamserver; GO111MODULE="on" go build -ldflags="-s -w -X cmd.VersionCommit=$(git rev-parse HEAD)" -o ../havoc main.go
	@ if sudo -n setcap 'cap_net_bind_service=+ep' havoc 2>/dev/null; then \
		echo "[*] setcap cap_net_bind_service 已应用（允许非 root 绑定 <1024 端口）"; \
	else \
		echo "[!] 跳过 setcap（需要 sudo；teamserver 默认 40056 端口无需该权限）"; \
	fi

dev-ts-compile:
	@ echo "[*] compile teamserver"
	@ cd teamserver; GO111MODULE="on" go build -ldflags="-s -w -X cmd.VersionCommit=$(git rev-parse HEAD)" -o ../havoc main.go 

ts-cleanup: 
	@ echo "[*] teamserver cleanup"
	@ rm -rf ./teamserver/bin
	@ rm -rf ./data/loot
	@ rm -rf ./data/x86_64-w64-mingw32-cross 
	@ rm -rf ./data/havoc.db
	@ rm -rf ./data/server.*
	@ rm -rf ./teamserver/.idea
	@ rm -rf ./havoc

# cleanup target 
clean: ts-cleanup
	@ rm -rf ./data/*.db
	@ rm -rf payloads/Demon/.idea
