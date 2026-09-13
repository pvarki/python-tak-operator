<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<TAKIgniteConfiguration xmlns="http://bbn.com/marti/xml/config"
    igniteHost="{{.Env.POD_IP}}"
    igniteNonMulticastDiscoveryPort="47500"
    igniteNonMulticastDiscoveryPortCount="10"
    igniteCommunicationPort="47100"
    igniteCommunicationPortCount="10"
    ignitePoolSize="4"
    cacheOffHeapInitialSizeBytes="33554432"
    cacheOffHeapMaxSizeBytes="134217728"/>
