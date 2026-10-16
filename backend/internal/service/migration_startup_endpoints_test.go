package service

import (
	"os"
	"os/exec"
	"path/filepath"
	"testing"
)

func TestMigrationStartupEndpointSelection(t *testing.T) {
	for _, raw := range []string{
		`{"v":1,"postgres":"target","redis":"source"}`,
		`{"v":1,"postgres":"target","redis":"target"}`,
	} {
		value, err := parseMigrationStartupEndpoints([]byte(raw))
		if err != nil || value.Postgres != "target" {
			t.Fatalf("selection %q: %+v %v", raw, value, err)
		}
	}
	for _, raw := range []string{
		`{}`, `{"v":2,"postgres":"target","redis":"source"}`,
		`{"v":1,"postgres":"other","redis":"source"}`,
		`{"v":1,"postgres":"target","redis":"source","address":"127.0.0.1"}`,
		`{"v":1,"postgres":"target","redis":"source"} {}`,
	} {
		if _, err := parseMigrationStartupEndpoints([]byte(raw)); err == nil {
			t.Fatalf("accepted invalid startup selection %q", raw)
		}
	}
}

func TestMigrationStartupEndpointSnapshotSurvivesFileReplacement(t *testing.T) {
	if path := os.Getenv("MIGRATION_ENDPOINT_TEST_CHILD"); path != "" {
		first, err := MigrationInitialEndpoints()
		if err != nil || first.Postgres != "target" || first.Redis != "source" {
			t.Fatalf("first snapshot: %+v %v", first, err)
		}
		replacement := path + ".new"
		if err := os.WriteFile(replacement, []byte(`{"v":1,"postgres":"target","redis":"target"}`), 0600); err != nil {
			t.Fatal(err)
		}
		if err := os.Rename(replacement, path); err != nil {
			t.Fatal(err)
		}
		second, err := MigrationInitialEndpoints()
		if err != nil || second != first {
			t.Fatalf("startup clients saw different checkpoints: %+v %+v %v", first, second, err)
		}
		return
	}
	path := filepath.Join(t.TempDir(), "endpoints.json")
	if err := os.WriteFile(path, []byte(`{"v":1,"postgres":"target","redis":"source"}`), 0600); err != nil {
		t.Fatal(err)
	}
	command := exec.Command(os.Args[0], "-test.run=^TestMigrationStartupEndpointSnapshotSurvivesFileReplacement$")
	command.Env = append(os.Environ(), "MIGRATION_ENDPOINT_TEST_CHILD="+path, "MIGRATION_ENDPOINT_STATE_FILE="+path)
	if output, err := command.CombinedOutput(); err != nil {
		t.Fatalf("isolated startup: %v %s", err, output)
	}
}
