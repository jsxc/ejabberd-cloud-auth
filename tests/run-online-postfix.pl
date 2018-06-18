#!/usr/bin/perl -w
use IPC::Open2;
if ( ! -r "/etc/xcauth.accounts" ) {
  print STDERR "/etc/xcauth.accounts must exist and be readable\n";
  exit(1);
}
$| = 1; # Autoflush on
open STDIN, "</etc/xcauth.accounts" or die;
my $pid = open2(\*PROG, \*COMMAND, "./xcauth.py", "-t", "postfix") or die "$!";
binmode(PROG);
binmode(COMMAND);
$u = '';
$d = '';
$p = '';
while (<>) {
  chomp;
  next if length($_) == 0 || substr($_, 0, 1) eq '#';
  @fields = split(/\t/, $_, -1);
  if ($#fields != 2) {
    print STDERR "Need 3 fields per line: $_\n";
    exit(1);
  }
  if ($fields[0] eq '') {
    if ($fields[1] eq 'auth') {
      print STDERR "Not testing auth command in postfix mode\n";
      next;
    } elsif ($fields[1] eq 'isuser') {
      $cmd = "get $u\@$d";
      print COMMAND "$cmd\n";
    } elsif ($fields[1] eq 'roster') {
      print STDERR "Not testing roster command in postfix mode\n";
      next;
    } else {
      print STDERR "Invalid command $fields[1]\n";
      exit(1);
    }
    $data = <PROG>;
    # Normalization
    chomp $data;
    if ($data =~ /^200 /) {
      $data = "True";
    } elsif ($data =~ /^500 /) {
      $data = "False";
    }

    if ($data ne $fields[2]) {
      print STDERR "*** Test " . join(' ', @fields[1,2]) . " failed ($u/$d/$p: $data != $fields[2])\n";
      exit(1);
    } else {
      print STDERR "*** Test " . join(' ', @fields[1,2]) . " succeeded\n\n";
    }
  } else {
    ($u, $d, $p) = @fields;
  }
}
