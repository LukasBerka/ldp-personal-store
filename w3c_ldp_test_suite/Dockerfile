FROM maven:3.8.6-openjdk-8 AS build

ARG LDP_TESTSUITE_SHA=8e7936888c619b03ed2caffeed9e26b151347ac0
WORKDIR /src

ADD https://github.com/w3c/ldp-testsuite/archive/${LDP_TESTSUITE_SHA}.tar.gz /tmp/src.tgz
RUN tar xzf /tmp/src.tgz --strip-components=1 -C /src

RUN sed -i '/<artifactId>commons-cli<\/artifactId>/{n;s#<version>1\.4</version>#<version>1.2</version>#}' pom.xml

RUN mvn -B -q -DskipTests -Dgpg.skip=true -Dmaven.javadoc.skip=true \
        -Denforcer.skip=true package

FROM eclipse-temurin:8-jre
COPY --from=build /src/target/ldp-testsuite-*-shaded.jar /ldp-testsuite.jar
COPY logback.xml /conf/logback.xml
WORKDIR /out
ENTRYPOINT ["java", "-Dlogback.configurationFile=/conf/logback.xml", "-jar", "/ldp-testsuite.jar"]
