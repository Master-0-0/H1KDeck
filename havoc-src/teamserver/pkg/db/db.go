package db

import (
	"database/sql"
	"os"

	_ "github.com/mattn/go-sqlite3"
)

type DB struct {
	existed bool
	db      *sql.DB
	path    string
}

func DatabaseNew(dbpath string) (*DB, error) {
	var (
		db  = new(DB)
		err error
	)

	db.path = dbpath

	db.existed = true
	if _, err = os.Stat(dbpath); os.IsNotExist(err) {
		db.existed = false
	}

	/* creates and or opens a db */
	db.db, err = sql.Open("sqlite3", db.path)
	if err != nil {
		return nil, err
	}

	if !db.existed {

		/* create db tables */
		err = db.init()
		if err != nil {
			return nil, err
		}

	}

	/* 旧库迁移：早期 TS_Agents 没有 Listener/Platform 列。
	 * 加列后 teamserver 重启仍能显示会话来自哪个监听器、以及 Linux 平台。 */
	if err = db.ensureColumn("TS_Agents", "Listener", "text"); err != nil {
		return nil, err
	}
	if err = db.ensureColumn("TS_Agents", "Platform", "text"); err != nil {
		return nil, err
	}

	return db, nil
}

func (db *DB) init() error {
	var err error

	_, err = db.db.Exec(`CREATE TABLE "TS_Listeners" ("Name" text UNIQUE, "Protocol" text, "Config" text);`)
	if err != nil {
		return err
	}

	_, err = db.db.Exec(`CREATE TABLE "TS_Agents" ("AgentID" int, "Active" int, "Reason" string, "AESKey" string, "AESIv" string, "Hostname" string, "Username" string, "DomainName" string, "ExternalIP" string, "InternalIP" string, "ProcessName" string, BaseAddress int, "ProcessPID" int, "ProcessTID" int, "ProcessPPID" int, "ProcessArch" string, "Elevated" string, "OSVersion" string, "OSArch" string, "SleepDelay" int, "SleepJitter" int, "KillDate" int, "WorkingHours" int, "FirstCallIn" string, "LastCallIn" string, "Listener" text, "Platform" text);`)
	if err != nil {
		return err
	}

	_, err = db.db.Exec(`CREATE TABLE "TS_Links" ("ParentAgentID" int, "LinkAgentID" int);`)
	if err != nil {
		return err
	}

	return nil
}

// ensureColumn 为已有数据库补列（SQLite 不支持 ADD COLUMN IF NOT EXISTS）。
func (db *DB) ensureColumn(table, column, decl string) error {
	rows, err := db.db.Query("PRAGMA table_info(" + table + ")")
	if err != nil {
		return err
	}

	found := false
	for rows.Next() {
		var (
			cid       int
			name      string
			ctype     string
			notnull   int
			dfltValue any
			pk        int
		)
		if err = rows.Scan(&cid, &name, &ctype, &notnull, &dfltValue, &pk); err != nil {
			rows.Close()
			return err
		}
		if name == column {
			found = true
		}
	}
	rows.Close()

	if found {
		return nil
	}

	_, err = db.db.Exec("ALTER TABLE " + table + " ADD COLUMN " + column + " " + decl)
	return err
}

func (db *DB) Existed() bool {
	return db.existed
}

func (db *DB) Path() string {
	return db.path
}
