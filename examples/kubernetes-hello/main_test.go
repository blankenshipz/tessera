package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func TestWriteSumSquares(t *testing.T) {
	for _, test := range []struct {
		iterations int64
		want       int64
	}{{10, 385}, {20, 2870}} {
		path := filepath.Join(t.TempDir(), "outputs", "result.txt")
		writeSumSquares(test.iterations, path)
		data, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		var got struct {
			Iterations int64 `json:"iterations"`
			SumSquares int64 `json:"sum_squares"`
		}
		if err := json.Unmarshal(data, &got); err != nil {
			t.Fatal(err)
		}
		if got.Iterations != test.iterations || got.SumSquares != test.want {
			t.Fatalf("got %+v, want iterations=%d sum_squares=%d", got, test.iterations, test.want)
		}
	}
}
