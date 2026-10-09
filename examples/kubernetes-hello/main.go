// kubernetes-hello is a deterministic, dependency-free E2E workload.
package main

import (
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"io/ioutil"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"syscall"
	"time"
)

var result = []byte("hello tessera kubernetes e2e\n")

func fail(err error) {
	fmt.Fprintln(os.Stderr, err)
	os.Exit(1)
}

func main() {
	if len(os.Args) < 2 {
		fail(fmt.Errorf("usage: /hello --write|--read path | --sleep-write seconds path"))
	}
	switch os.Args[1] {
	case "--write":
		write(os.Args[2])
	case "--cpu-sum-squares":
		if len(os.Args) != 4 {
			fail(fmt.Errorf("usage: /hello --cpu-sum-squares iterations /outputs/result.txt"))
		}
		iterations, err := strconv.ParseInt(os.Args[2], 10, 64)
		if err != nil || iterations < 1 || iterations > 100000 {
			fail(fmt.Errorf("iterations must be between 1 and 100000"))
		}
		writeSumSquares(iterations, os.Args[3])
	case "--sleep-write":
		if len(os.Args) != 4 {
			fail(fmt.Errorf("usage: /hello --sleep-write seconds /outputs/result.txt"))
		}
		seconds, err := strconv.Atoi(os.Args[2])
		if err != nil || seconds < 1 || seconds > 120 {
			fail(fmt.Errorf("sleep seconds must be between 1 and 120"))
		}
		time.Sleep(time.Duration(seconds) * time.Second)
		write(os.Args[3])
	case "--read":
		if len(os.Args) != 3 {
			fail(fmt.Errorf("usage: /hello --read /outputs/result.txt"))
		}
		value, err := ioutil.ReadFile(os.Args[2])
		if err != nil {
			fail(err)
		}
		if _, err = os.Stdout.Write(value); err != nil {
			fail(err)
		}
	case "--http-server":
		if len(os.Args) != 2 {
			fail(fmt.Errorf("usage: /hello --http-server"))
		}
		http.HandleFunc("/", func(writer http.ResponseWriter, _ *http.Request) {
			_, _ = writer.Write([]byte("tessera-network-probe\n"))
		})
		fail(http.ListenAndServe(":8080", nil))
	case "--http-probe":
		if len(os.Args) != 4 || (os.Args[3] != "allow" && os.Args[3] != "deny") {
			fail(fmt.Errorf("usage: /hello --http-probe host:port allow|deny"))
		}
		probeHTTP(os.Args[2], os.Args[3])
	case "--write-sentinel":
		if len(os.Args) != 4 {
			fail(fmt.Errorf("usage: /hello --write-sentinel path value"))
		}
		writeSentinel(os.Args[2], []byte(os.Args[3]+"\n"))
	case "--verify-sentinel":
		if len(os.Args) != 4 {
			fail(fmt.Errorf("usage: /hello --verify-sentinel path value"))
		}
		verifySentinel(os.Args[2], []byte(os.Args[3]+"\n"), false)
	case "--verify-sentinel-and-cleanup":
		if len(os.Args) != 4 {
			fail(fmt.Errorf("usage: /hello --verify-sentinel-and-cleanup path value"))
		}
		verifySentinel(os.Args[2], []byte(os.Args[3]+"\n"), true)
	case "--probe-byte-quota":
		if len(os.Args) != 4 {
			fail(fmt.Errorf("usage: /hello --probe-byte-quota /outputs/payload limit-bytes"))
		}
		limit, err := boundedQuota(os.Args[3], 64*1024*1024)
		if err != nil {
			fail(err)
		}
		probeByteQuota(os.Args[2], limit)
	case "--probe-inode-quota":
		if len(os.Args) != 4 {
			fail(fmt.Errorf("usage: /hello --probe-inode-quota /outputs/directory limit-files"))
		}
		limit, err := boundedQuota(os.Args[3], 10000)
		if err != nil {
			fail(err)
		}
		probeInodeQuota(os.Args[2], limit)
	default:
		fail(fmt.Errorf("unsupported mode"))
	}
}

// writeSumSquares performs bounded deterministic CPU work and stores its actual
// computed result in a compact JSON record suitable for downstream measurement.
func writeSumSquares(iterations int64, path string) {
	var sum int64
	for i := int64(1); i <= iterations; i++ {
		sum += i * i
	}
	encoded, err := json.Marshal(struct {
		Iterations int64 `json:"iterations"`
		SumSquares int64 `json:"sum_squares"`
	}{iterations, sum})
	if err != nil {
		fail(err)
	}
	if err := os.MkdirAll(filepath.Dir(path), 0755); err != nil {
		fail(err)
	}
	if err := ioutil.WriteFile(path, append(encoded, '\n'), 0644); err != nil {
		fail(err)
	}
}

func boundedQuota(value string, maximum int64) (int64, error) {
	limit, err := strconv.ParseInt(value, 10, 64)
	if err != nil || limit < 1 || limit > maximum {
		return 0, fmt.Errorf("quota limit must be between 1 and %d", maximum)
	}
	return limit, nil
}

func quotaErrno(err error) int {
	var errno syscall.Errno
	if errors.As(err, &errno) {
		return int(errno)
	}
	return 0
}

func quotaRecord(record map[string]interface{}) {
	encoded, err := json.Marshal(record)
	if err != nil {
		fail(err)
	}
	fmt.Println(string(encoded))
}

// probeByteQuota writes non-sparse data past limit, records the actual failure,
// and removes its payload so a distinct inode probe can run on the same new PVC.
func probeByteQuota(path string, limit int64) {
	if err := os.MkdirAll("/outputs", 0700); err != nil {
		fail(err)
	}
	requested := limit + 65536
	file, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	if err != nil {
		fail(err)
	}
	buffer := make([]byte, 65536)
	for index := range buffer {
		buffer[index] = byte(index % 251)
	}
	written := int64(0)
	var failure error
	for written < requested {
		count := int64(len(buffer))
		if remaining := requested - written; remaining < count {
			count = remaining
		}
		amount, writeErr := file.Write(buffer[:int(count)])
		written += int64(amount)
		if writeErr != nil {
			failure = writeErr
			break
		}
		if int64(amount) != count {
			failure = fmt.Errorf("short non-sparse write: %d of %d", amount, count)
			break
		}
	}
	if failure == nil {
		failure = file.Sync()
	}
	if closeErr := file.Close(); failure == nil {
		failure = closeErr
	}
	record := map[string]interface{}{"kind": "byte", "limit": limit, "requested_bytes": requested, "written_bytes": written}
	if failure == nil {
		record["outcome"] = "not_enforced"
	} else {
		record["outcome"] = "enforced"
		record["failure"] = failure.Error()
		record["failure_errno"] = quotaErrno(failure)
	}
	if err := os.Remove(path); err != nil {
		record["cleanup_error"] = err.Error()
	}
	quotaRecord(record)
}

// probeInodeQuota creates files past limit, records the actual failure, and
// removes every created file before returning.
func probeInodeQuota(directory string, limit int64) {
	if err := os.MkdirAll(directory, 0700); err != nil {
		fail(err)
	}
	requested := limit + 1
	created := int64(0)
	var failure error
	for created < requested {
		path := fmt.Sprintf("%s/file-%06d", directory, created)
		file, createErr := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
		if createErr != nil {
			failure = createErr
			break
		}
		if closeErr := file.Close(); closeErr != nil {
			failure = closeErr
			break
		}
		created++
	}
	record := map[string]interface{}{"kind": "inode", "limit": limit, "requested_files": requested, "created_files": created}
	if failure == nil {
		record["outcome"] = "not_enforced"
	} else {
		record["outcome"] = "enforced"
		record["failure"] = failure.Error()
		record["failure_errno"] = quotaErrno(failure)
	}
	for index := int64(0); index < created; index++ {
		if err := os.Remove(fmt.Sprintf("%s/file-%06d", directory, index)); err != nil {
			record["cleanup_error"] = err.Error()
			break
		}
	}
	if err := os.Remove(directory); err != nil {
		record["cleanup_error"] = err.Error()
	}
	quotaRecord(record)
}

func probeHTTP(address string, expected string) {
	client := &http.Client{Timeout: 3 * time.Second}
	response, err := client.Get("http://" + address + "/")
	if expected == "deny" {
		if err == nil {
			_ = response.Body.Close()
			fail(fmt.Errorf("unexpected connection to denied destination: HTTP %d", response.StatusCode))
		}
		fmt.Printf("denied connection observed: %v\n", err)
		return
	}
	if err != nil {
		fail(fmt.Errorf("allowed connection failed: %w", err))
	}
	defer response.Body.Close()
	body, err := ioutil.ReadAll(response.Body)
	if err != nil || response.StatusCode != http.StatusOK || string(body) != "tessera-network-probe\n" {
		fail(fmt.Errorf("allowed connection returned unexpected response"))
	}
	fmt.Println("allowed connection observed")
}

func writeSentinel(path string, value []byte) {
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		fail(err)
	}
	if err := ioutil.WriteFile(path, value, 0600); err != nil {
		fail(err)
	}
	checksum := fmt.Sprintf("%x\n", sha256.Sum256(value))
	if err := ioutil.WriteFile(path+".sha256", []byte(checksum), 0600); err != nil {
		fail(err)
	}
	fmt.Print(checksum)
}

func verifySentinel(path string, expected []byte, cleanup bool) {
	value, err := ioutil.ReadFile(path)
	if err != nil || string(value) != string(expected) {
		fail(fmt.Errorf("sentinel did not survive remount"))
	}
	checksum, err := ioutil.ReadFile(path + ".sha256")
	expectedChecksum := fmt.Sprintf("%x\n", sha256.Sum256(expected))
	if err != nil || string(checksum) != expectedChecksum {
		fail(fmt.Errorf("sentinel checksum did not survive remount"))
	}
	if cleanup {
		if err := os.Remove(path); err != nil {
			fail(err)
		}
		if err := os.Remove(path + ".sha256"); err != nil {
			fail(err)
		}
	}
	fmt.Print(expectedChecksum)
}

func write(path string) {
	if err := os.MkdirAll("/outputs", 0700); err != nil {
		fail(err)
	}
	file, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	if err != nil {
		fail(err)
	}
	if _, err = file.Write(result); err != nil {
		_ = file.Close()
		fail(err)
	}
	if err = file.Close(); err != nil {
		fail(err)
	}
}
