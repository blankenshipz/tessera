// kubernetes-hello is a deterministic, dependency-free E2E workload.
package main

import (
	"fmt"
	"io/ioutil"
	"os"
)

var result = []byte("hello tessera kubernetes e2e\n")

func fail(err error) {
	fmt.Fprintln(os.Stderr, err)
	os.Exit(1)
}

func main() {
	if len(os.Args) != 3 {
		fail(fmt.Errorf("usage: /hello --write|--read /outputs/result.txt"))
	}
	switch os.Args[1] {
	case "--write":
		if err := os.MkdirAll("/outputs", 0700); err != nil {
			fail(err)
		}
		file, err := os.OpenFile(os.Args[2], os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
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
	case "--read":
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
