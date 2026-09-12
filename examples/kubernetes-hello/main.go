// kubernetes-hello is a deterministic, dependency-free E2E workload.
package main

import (
	"fmt"
	"io/ioutil"
	"os"
	"strconv"
	"time"
)

var result = []byte("hello tessera kubernetes e2e\n")

func fail(err error) {
	fmt.Fprintln(os.Stderr, err)
	os.Exit(1)
}

func main() {
	if len(os.Args) != 3 && len(os.Args) != 4 {
		fail(fmt.Errorf("usage: /hello --write|--read path | --sleep-write seconds path"))
	}
	switch os.Args[1] {
	case "--write":
		write(os.Args[2])
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
	default:
		fail(fmt.Errorf("unsupported mode"))
	}
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
